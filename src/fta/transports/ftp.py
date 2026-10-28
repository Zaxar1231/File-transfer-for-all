"""FTP and FTPS (explicit TLS, RFC 4217)."""

from __future__ import annotations

import ftplib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fta.core.errors import NotFound
from fta.core.registry import register
from fta.core.transport import (
    CHUNK_SIZE,
    Progress,
    ProgressCallback,
    RemoteEntry,
    Transport,
    join_path,
    open_for_resume,
    translate_errors,
)


@register
class FtpTransport(Transport):
    """Options: username, password, timeout. Listing and stat need MLSD/MLST (RFC 3659)."""

    schemes = ("ftp", "ftps")
    library_errors = (ftplib.Error,)
    can_resume_download = True
    can_resume_upload = True  # REST before STOR; servers without it answer with an error

    _ftp: ftplib.FTP | None = None

    @translate_errors
    def connect(self) -> None:
        ep = self.endpoint
        ftp_class = ftplib.FTP_TLS if ep.scheme == "ftps" else ftplib.FTP
        ftp = ftp_class(timeout=self.float_option("timeout", 30))
        ftp.connect(ep.host or "", ep.port or 21)
        ftp.login(self.username or "anonymous", self.password or "")
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()  # encrypt the data channel too
        self._ftp = ftp

    def close(self) -> None:
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except ftplib.all_errors:
            self._ftp.close()
        finally:
            self._ftp = None

    @translate_errors
    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        tracker = Progress(progress, self._size(remote), done=offset)
        with open_for_resume(local, offset) as out:

            def write(chunk: bytes) -> None:
                out.write(chunk)
                tracker.advance(len(chunk))

            # REST <offset> before RETR makes the server start mid-file
            self._session.retrbinary(f"RETR {remote}", write, blocksize=CHUNK_SIZE, rest=offset or None)

    @translate_errors
    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        tracker = Progress(progress, local.stat().st_size, done=offset)
        with local.open("rb") as fp:
            fp.seek(offset)
            self._session.storbinary(
                f"STOR {remote}", tracker.wrap_reader(fp), blocksize=CHUNK_SIZE, rest=offset or None
            )

    @translate_errors
    def listdir(self, remote: str) -> list[RemoteEntry]:
        if self._mlsd:
            try:
                entries = []
                for name, facts in self._session.mlsd(remote or "/"):
                    if facts.get("type") in ("cdir", "pdir"):  # "." and ".."
                        continue
                    entries.append(_entry(name, join_path(remote, name), facts))
                return entries
            except ftplib.error_perm as exc:
                if not str(exc).startswith(("500", "502", "504")):
                    raise
                self._mlsd = False  # vsftpd and many routers/NAS: no MLSD, read LIST instead
        lines: list[str] = []
        self._session.retrlines(f"LIST {remote or '/'}", lines.append)
        return [entry for line in lines if (entry := parse_list_line(line, remote)) is not None]

    @translate_errors
    def stat(self, remote: str) -> RemoteEntry:
        if self._mlsd:
            try:
                reply = self._session.sendcmd(f"MLST {remote or '/'}")
            except ftplib.error_perm as exc:
                if not str(exc).startswith(("500", "502", "504")):
                    raise NotFound(f"{remote}: {exc}") from exc
                self._mlsd = False
            else:
                # The facts line is the second line of the reply: " type=file;size=5; /path"
                facts_text = reply.splitlines()[1].strip().partition(" ")[0]
                facts = dict(fact.partition("=")[::2] for fact in facts_text.split(";") if fact)
                return _entry(PurePosixPath(remote).name, remote, {k.lower(): v for k, v in facts.items()})
        path = PurePosixPath(remote or "/")
        if str(path) == "/":
            return RemoteEntry(name="", path="/", is_dir=True)
        for entry in self.listdir(str(path.parent)):
            if entry.name == path.name:
                return entry
        raise NotFound(f"{remote}: no such file or directory")

    @translate_errors
    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        try:
            self._session.mkd(remote)
        except ftplib.error_perm:
            if exist_ok and self._is_dir(remote):
                return
            raise

    @translate_errors
    def delete(self, remote: str) -> None:
        if self._is_dir(remote):
            self._session.rmd(remote)
        else:
            self._session.delete(remote)

    _mlsd = True
    """Cleared when the server answers MLSD/MLST with "unknown command"."""

    @property
    def _session(self) -> ftplib.FTP:
        if self._ftp is None:
            raise RuntimeError("FTP session is not open; use 'with' or call connect()")
        return self._ftp

    def _is_dir(self, remote: str) -> bool:
        try:
            return self.stat(remote).is_dir
        except NotFound:
            return False

    def _size(self, remote: str) -> int | None:
        try:
            # Many servers refuse SIZE in ASCII mode, which ftplib starts in.
            self._session.voidcmd("TYPE I")
            return self._session.size(remote)
        except ftplib.error_perm:
            return None


def _entry(name: str, path: str, facts: dict[str, str]) -> RemoteEntry:
    is_dir = facts.get("type") in ("dir", "cdir")
    size = facts.get("size")
    return RemoteEntry(
        name=name,
        path=path,
        is_dir=is_dir,
        size=int(size) if size is not None and not is_dir else None,
        modified=_parse_modify(facts.get("modify")),
    )


def _parse_modify(value: str | None) -> float | None:
    """MLSx "modify" facts are UTC timestamps like 20261027073000[.123]."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_list_line(line: str, parent: str) -> RemoteEntry | None:
    """One line of a LIST reply, Unix ``ls -l`` style or Windows/IIS style; None for noise."""
    import datetime

    parts = line.split(None, 8)
    if len(parts) == 9 and parts[0][:1] in "-dlbcps":  # drwxr-xr-x 2 user group 4096 Oct 27 20:52 name
        mode, size, month, day, time_or_year, name = parts[0], parts[4], parts[5], parts[6], parts[7], parts[8]
        if mode.startswith("l") and " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            return None
        modified = None
        try:
            now = datetime.datetime.now()
            if ":" in time_or_year:
                hour, minute = (int(x) for x in time_or_year.split(":"))
                stamp = datetime.datetime(now.year, _MONTHS[month.lower()[:3]], int(day), hour, minute)
                if stamp > now + datetime.timedelta(days=1):  # "Dec 31 23:59" seen in January is last year
                    stamp = stamp.replace(year=now.year - 1)
            else:
                stamp = datetime.datetime(int(time_or_year), _MONTHS[month.lower()[:3]], int(day))
            modified = stamp.timestamp()
        except (KeyError, ValueError):
            pass
        is_dir = mode.startswith("d")
        return RemoteEntry(name, join_path(parent, name), is_dir, None if is_dir else _int(size), modified)
    parts = line.split(None, 3)
    if len(parts) == 4 and "-" in parts[0] and parts[1][-2:].upper() in ("AM", "PM"):  # 10-27-26  08:52PM <DIR> name
        date_text, time_text, size, name = parts
        modified = None
        try:
            modified = datetime.datetime.strptime(f"{date_text} {time_text.upper()}", "%m-%d-%y %I:%M%p").timestamp()
        except ValueError:
            pass
        is_dir = size.upper() == "<DIR>"
        return RemoteEntry(name, join_path(parent, name), is_dir, None if is_dir else _int(size), modified)
    return None


def _int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None
