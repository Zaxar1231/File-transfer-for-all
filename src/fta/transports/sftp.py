"""SFTP (SSH File Transfer Protocol) via paramiko."""

from __future__ import annotations

import stat as stat_mode
from pathlib import Path, PurePosixPath
from typing import Any

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import ProgressCallback, RemoteEntry, Transport, join_path, translate_errors


@register
class SftpTransport(Transport):
    """Options: username, password, key_file, known_hosts, accept_unknown_host, timeout.

    Host keys are checked against ~/.ssh/known_hosts (plus ``known_hosts`` if
    given); unknown hosts are rejected unless ``accept_unknown_host=true``.
    """

    schemes = ("sftp",)

    _client: Any = None
    _sftp: Any = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError:
            raise TransferError('sftp:// needs paramiko: pip install "fta[sftp]"') from None
        self.library_errors = (paramiko.SSHException,)

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if known_hosts := self.options.get("known_hosts"):
            client.load_host_keys(known_hosts)
        accept = self.bool_option("accept_unknown_host")
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy() if accept else paramiko.RejectPolicy())
        try:
            client.connect(
                self.endpoint.host or "",
                port=self.endpoint.port or 22,
                username=self.username,
                password=self.password,
                key_filename=self.options.get("key_file"),
                timeout=self.float_option("timeout", 30),
            )
            self._sftp = client.open_sftp()
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise TransferError(f"sftp: cannot connect to {self.endpoint.netloc}: {exc}") from exc
        self._client = client

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        if self._client is not None:
            self._client.close()
        self._sftp = self._client = None

    @translate_errors
    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        # paramiko's callback already has the (done, total) shape
        self._session.put(str(local), remote, callback=progress)

    @translate_errors
    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        self._session.get(remote, str(local), callback=progress)

    @translate_errors
    def listdir(self, remote: str) -> list[RemoteEntry]:
        base = remote or "."
        return [_entry(attr.filename, join_path(base, attr.filename), attr) for attr in self._session.listdir_attr(base)]

    @translate_errors
    def stat(self, remote: str) -> RemoteEntry:
        try:
            attr = self._session.stat(remote or ".")
        except FileNotFoundError as exc:
            raise NotFound(f"{remote}: no such file or directory") from exc
        return _entry(PurePosixPath(remote).name, remote, attr)

    @translate_errors
    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        try:
            self._session.mkdir(remote)
        except OSError:
            if exist_ok and self._is_dir(remote):
                return
            raise

    @translate_errors
    def delete(self, remote: str) -> None:
        if self._is_dir(remote):
            self._session.rmdir(remote)
        else:
            self._session.remove(remote)

    @property
    def _session(self) -> Any:
        if self._sftp is None:
            raise RuntimeError("SFTP session is not open; use 'with' or call connect()")
        return self._sftp

    def _is_dir(self, remote: str) -> bool:
        try:
            return self.stat(remote).is_dir
        except NotFound:
            return False


def _entry(name: str, path: str, attr: Any) -> RemoteEntry:
    is_dir = stat_mode.S_ISDIR(attr.st_mode or 0)
    return RemoteEntry(
        name=name,
        path=path,
        is_dir=is_dir,
        size=None if is_dir else attr.st_size,
        modified=attr.st_mtime,
    )
