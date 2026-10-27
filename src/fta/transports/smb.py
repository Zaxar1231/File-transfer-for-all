"""SMB 2/3 (Windows file sharing, Samba, most NAS boxes) via smbprotocol.

URLs look like ``smb://user@server/share/folder/file``: the first path segment
is the share name.
"""

from __future__ import annotations

import errno
import stat as stat_mode
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import (
    Progress,
    ProgressCallback,
    RemoteEntry,
    Transport,
    copy_stream,
    join_path,
    translate_errors,
)


@register
class SmbTransport(Transport):
    """Options: username, password, encrypt, timeout.

    A Windows domain account is given as ``DOMAIN\\user`` in the username option.
    """

    schemes = ("smb",)

    _smb: Any = None
    _cache: dict[str, Any] | None = None

    def connect(self) -> None:
        try:
            import smbclient
            from smbprotocol.exceptions import SMBException
        except ImportError:
            raise TransferError("smb:// needs smbprotocol: pip install smbprotocol") from None
        self._smb = smbclient
        self.library_errors = (SMBException,)
        self._cache = {}
        try:
            smbclient.register_session(
                self.endpoint.host or "",
                username=self.username,
                password=self.password,
                port=self._port,
                encrypt=True if self.bool_option("encrypt") else None,
                connection_timeout=int(self.float_option("timeout", 30)),
                connection_cache=self._cache,
            )
        except (SMBException, ValueError, OSError) as exc:  # ValueError: connection refused
            self.close()
            raise TransferError(f"smb: cannot connect to {self.endpoint.netloc}: {exc}") from exc

    def close(self) -> None:
        if self._cache is not None and self._smb is not None:
            self._smb.reset_connection_cache(connection_cache=self._cache)
        self._cache = None

    @translate_errors
    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        tracker = Progress(progress, local.stat().st_size)
        with local.open("rb") as reader, self._smb.open_file(self._unc(remote), mode="wb", **self._kw) as writer:
            copy_stream(reader, writer, tracker)

    @translate_errors
    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        unc = self._unc(remote)
        tracker = Progress(progress, self._smb.stat(unc, **self._kw).st_size)
        with self._smb.open_file(unc, mode="rb", **self._kw) as reader, local.open("wb") as writer:
            copy_stream(reader, writer, tracker)

    @translate_errors
    def listdir(self, remote: str) -> list[RemoteEntry]:
        # Read the listing's own metadata: SMBDirEntry.stat() would reconnect
        # without our port and connection cache.
        entries = []
        for item in self._smb.scandir(self._unc(remote), **self._kw):
            info = item.smb_info
            is_dir = bool(info.file_attributes & _FILE_ATTRIBUTE_DIRECTORY)
            entries.append(
                RemoteEntry(
                    name=item.name,
                    path=join_path(remote, item.name),
                    is_dir=is_dir,
                    size=None if is_dir else info.end_of_file,
                    modified=_timestamp(info.last_write_time),
                )
            )
        return entries

    @translate_errors
    def stat(self, remote: str) -> RemoteEntry:
        try:
            info = self._smb.stat(self._unc(remote), **self._kw)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise NotFound(f"{remote}: no such file or directory") from exc
            raise
        return _entry(PurePosixPath(remote).name, remote, info)

    @translate_errors
    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        try:
            self._smb.mkdir(self._unc(remote), **self._kw)
        except OSError:
            if exist_ok and self._is_dir(remote):
                return
            raise

    @translate_errors
    def delete(self, remote: str) -> None:
        if self._is_dir(remote):
            self._smb.rmdir(self._unc(remote), **self._kw)
        else:
            self._smb.remove(self._unc(remote), **self._kw)

    @property
    def _port(self) -> int:
        return self.endpoint.port or 445

    @property
    def _kw(self) -> dict[str, Any]:
        if self._cache is None:
            raise RuntimeError("SMB session is not open; use 'with' or call connect()")
        return {"port": self._port, "connection_cache": self._cache}

    def _unc(self, remote: str) -> str:
        """``/share/dir/file`` → ``\\\\server\\share\\dir\\file``."""
        parts = [part for part in remote.split("/") if part]
        if not parts:
            raise TransferError("smb URLs need a share: smb://server/share/path")
        return "\\\\" + "\\".join([self.endpoint.host or "", *parts])

    def _is_dir(self, remote: str) -> bool:
        try:
            return self.stat(remote).is_dir
        except NotFound:
            return False


_FILE_ATTRIBUTE_DIRECTORY = 0x10


def _timestamp(value: datetime) -> float:
    """SMB times come back as datetimes; naive ones are UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _entry(name: str, path: str, info: Any) -> RemoteEntry:
    is_dir = stat_mode.S_ISDIR(info.st_mode)
    return RemoteEntry(
        name=name,
        path=path,
        is_dir=is_dir,
        size=None if is_dir else info.st_size,
        modified=info.st_mtime,
    )
