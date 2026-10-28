"""SFTP (SSH File Transfer Protocol) via paramiko."""

from __future__ import annotations

import stat as stat_mode
from pathlib import Path, PurePosixPath
from typing import Any

from fta.core import hostkeys
from fta.core.errors import NotFound, TransferError
from fta.core.hostkeys import HostKeyChanged, UnknownHostKey
from fta.core.registry import register
from fta.core.transport import (
    Progress,
    ProgressCallback,
    RemoteEntry,
    Transport,
    copy_stream,
    join_path,
    open_for_resume,
    patch_stream,
    translate_errors,
)


@register
class SftpTransport(Transport):
    """Options: username, password, key_file, known_hosts, accept_unknown_host, timeout.

    Host keys are checked against ~/.ssh/known_hosts and the app's known_hosts
    (``known_hosts`` overrides it). An unknown host raises UnknownHostKey with its
    fingerprint for the user to confirm, unless ``accept_unknown_host=true``.
    """

    schemes = ("sftp",)
    can_resume_download = True
    can_resume_upload = True
    can_patch = True

    _client: Any = None
    _sftp: Any = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError:
            raise TransferError("sftp:// needs paramiko: pip install paramiko") from None
        self.library_errors = (paramiko.SSHException,)

        client = paramiko.SSHClient()
        known_hosts = self.options.get("known_hosts")
        try:
            if self.bool_option("accept_unknown_host"):
                client.load_system_host_keys()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(self.endpoint.host or "", port=self.endpoint.port or 22, **self._login())
            else:
                hostkeys.connect(
                    client,
                    self.endpoint.host or "",
                    self.endpoint.port or 22,
                    Path(known_hosts) if known_hosts else None,
                    **self._login(),
                )
            self._sftp = client.open_sftp()
        except (UnknownHostKey, HostKeyChanged):
            client.close()
            raise
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise TransferError(f"sftp: cannot connect to {self.endpoint.netloc}: {exc}") from exc
        self._client = client

    def _login(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password": self.password,
            "key_filename": self.options.get("key_file"),
            "timeout": self.float_option("timeout", 30),
        }

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
        if self._client is not None:
            self._client.close()
        self._sftp = self._client = None

    @translate_errors
    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        if not offset:
            # paramiko's callback already has the (done, total) shape
            self._session.put(str(local), remote, callback=progress)
            return
        tracker = Progress(progress, local.stat().st_size, done=offset)
        with local.open("rb") as reader, self._session.open(remote, "r+b") as writer:
            writer.set_pipelined(True)
            reader.seek(offset)
            writer.seek(offset)
            copy_stream(reader, writer, tracker)

    @translate_errors
    def patch(
        self, local: Path, remote: str, ranges: list[tuple[int, int]], size: int, progress: ProgressCallback | None = None
    ) -> None:
        with self._session.open(remote, "r+b") as writer:
            writer.set_pipelined(True)
            patch_stream(local, writer, ranges, Progress(progress, sum(end - start for start, end in ranges)))
        self._session.truncate(remote, size)

    @translate_errors
    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        if not offset:
            self._session.get(remote, str(local), callback=progress)
            return
        size = self._session.stat(remote).st_size
        with self._session.open(remote, "rb") as reader, open_for_resume(local, offset) as writer:
            reader.seek(offset)
            reader.prefetch(size - offset)
            copy_stream(reader, writer, Progress(progress, size, done=offset))

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
