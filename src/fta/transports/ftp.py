"""FTP and FTPS (explicit TLS, RFC 4217)."""

from __future__ import annotations

import ftplib
from pathlib import Path

from fta.core.registry import register
from fta.core.transport import CHUNK_SIZE, Progress, ProgressCallback, RemoteEntry, Transport


@register
class FtpTransport(Transport):
    schemes = ("ftp", "ftps")

    _ftp: ftplib.FTP | None = None

    def connect(self) -> None:
        ep = self.endpoint
        ftp_class = ftplib.FTP_TLS if ep.scheme == "ftps" else ftplib.FTP
        ftp = ftp_class(timeout=self.options.get("timeout", 30))
        ftp.connect(ep.host, ep.port or 21)
        ftp.login(ep.username or "anonymous", ep.password or "")
        if ep.scheme == "ftps":
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

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        tracker = Progress(progress, self._size(remote))
        with local.open("wb") as out:

            def write(chunk: bytes) -> None:
                out.write(chunk)
                tracker.advance(len(chunk))

            self._session.retrbinary(f"RETR {remote}", write, blocksize=CHUNK_SIZE)

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        tracker = Progress(progress, local.stat().st_size)
        with local.open("rb") as fp:
            self._session.storbinary(f"STOR {remote}", tracker.wrap_reader(fp), blocksize=CHUNK_SIZE)

    def listdir(self, remote: str) -> list[RemoteEntry]:
        entries = []
        for name, facts in self._session.mlsd(remote):
            if facts.get("type") in ("cdir", "pdir"):  # "." and ".."
                continue
            size = facts.get("size")
            entries.append(
                RemoteEntry(
                    name=name,
                    path=f"{remote.rstrip('/')}/{name}",
                    is_dir=facts.get("type") == "dir",
                    size=int(size) if size is not None else None,
                )
            )
        return entries

    def mkdir(self, remote: str) -> None:
        self._session.mkd(remote)

    def delete(self, remote: str) -> None:
        self._session.delete(remote)

    @property
    def _session(self) -> ftplib.FTP:
        if self._ftp is None:
            raise RuntimeError("FTP session is not open; use 'with' or call connect()")
        return self._ftp

    def _size(self, remote: str) -> int | None:
        try:
            return self._session.size(remote)
        except ftplib.error_perm:
            return None
