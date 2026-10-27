"""Cloud drives through rclone: ``rclone://remote/path`` for any remote rclone knows.

Google Drive, Dropbox, Яндекс.Диск, OneDrive, Mail.ru and 70 more: the remote is
set up once (the app runs ``rclone config create``), then this transport drives
the rclone program. Uploads stream through ``rclone rcat`` and downloads through
``rclone cat``, so progress is counted here byte by byte; ``cat --offset``
continues a partial download.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import CHUNK_SIZE, Progress, ProgressCallback, RemoteEntry, Transport, join_path, open_for_resume
from fta.tools import rclone


@register
class RcloneTransport(Transport):
    """Options: none needed; the remote's settings live in rclone's config."""

    schemes = ("rclone",)
    can_resume_download = True
    can_resume_upload = False  # clouds take whole files; a partial upload is sent again

    def connect(self) -> None:
        self.remote = self.endpoint.host_as_written or self.endpoint.host or ""
        if not self.remote:
            raise TransferError("rclone:// needs a remote name, e.g. rclone://gdrive/Documents")
        if rclone.find() is None:
            raise TransferError("rclone is not installed; install it in Settings or from https://rclone.org")

    def close(self) -> None:
        pass

    def _target(self, path: str) -> str:
        return f"{self.remote}:{path.lstrip('/')}"

    def _run(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return rclone.run(args, remote=self.remote, **kwargs)

    # --- listing -------------------------------------------------------------------------

    def listdir(self, remote: str) -> list[RemoteEntry]:
        result = self._run("lsjson", self._target(remote), check=False)
        if result.returncode != 0:
            self._raise(result, remote)
        return [_entry(item, join_path(remote or "/", item["Name"])) for item in json.loads(result.stdout or b"[]")]

    def stat(self, remote: str) -> RemoteEntry:
        if remote.strip("/") == "":
            return RemoteEntry(name="", path="/", is_dir=True)
        result = self._run("lsjson", "--stat", self._target(remote), check=False)
        if result.returncode != 0:
            self._raise(result, remote)
        return _entry(json.loads(result.stdout), remote)

    def _raise(self, result: subprocess.CompletedProcess[bytes], path: str) -> None:
        text = result.stderr.decode(errors="replace")
        if "didn't find section in config" in text:
            raise TransferError(f"rclone: the cloud {self.remote!r} is not set up; add it under Connections → Cloud")
        if "not found" in text.lower() or "doesn't exist" in text.lower() or result.returncode == 3:
            raise NotFound(f"{self.remote}:{path}: no such file or directory")
        raise TransferError(f"rclone {self.remote}:{path}: {rclone._message(result.stderr)}")

    # --- files --------------------------------------------------------------------------

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        size = local.stat().st_size
        tracker = Progress(progress, size)
        process = rclone.popen(
            ["rcat", "--size", str(size), self._target(remote)],
            remote=self.remote,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with local.open("rb") as reader:
                while chunk := reader.read(CHUNK_SIZE):
                    process.stdin.write(chunk)
                    tracker.advance(len(chunk))
            process.stdin.close()
        except BrokenPipeError:
            pass  # rclone stopped reading; its error message says why
        except BaseException:
            process.kill()
            process.wait()
            raise
        _, stderr = process.communicate()
        if process.returncode != 0:
            raise TransferError(f"rclone upload to {self.remote}:{remote}: {rclone._message(stderr)}")

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        size = self.stat(remote).size
        tracker = Progress(progress, size, done=offset)
        args = ["cat", self._target(remote)] + (["--offset", str(offset)] if offset else [])
        process = rclone.popen(args, remote=self.remote, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with open_for_resume(local, offset) as writer:
                while chunk := process.stdout.read(CHUNK_SIZE):
                    writer.write(chunk)
                    tracker.advance(len(chunk))
        except BaseException:
            process.kill()
            process.wait()
            raise
        stderr = process.stderr.read()
        if process.wait() != 0:
            raise TransferError(f"rclone download of {self.remote}:{remote}: {rclone._message(stderr)}")

    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        if not exist_ok:
            try:
                self.stat(remote)
            except NotFound:
                pass
            else:
                raise TransferError(f"{self.remote}:{remote} already exists")
        self._run("mkdir", self._target(remote))

    def delete(self, remote: str) -> None:
        entry = self.stat(remote)
        self._run("rmdir" if entry.is_dir else "deletefile", self._target(remote))


def _entry(item: dict[str, Any], path: str) -> RemoteEntry:
    is_dir = bool(item.get("IsDir"))
    size = item.get("Size")
    return RemoteEntry(
        name=item.get("Name") or PurePosixPath(path).name,
        path=path,
        is_dir=is_dir,
        size=None if is_dir or size is None or size < 0 else int(size),
        modified=parse_time(item.get("ModTime") or ""),
    )


_TIME = re.compile(r"^(?P<base>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(?P<frac>\d+))?(?P<zone>Z|[+-]\d\d:\d\d)?$")


def parse_time(text: str) -> float | None:
    """rclone's RFC 3339 times carry nanoseconds and ``Z``; Python 3.10 reads neither."""
    match = _TIME.match(text)
    if not match:
        return None
    zone = match["zone"] or "+00:00"
    fraction = (match["frac"] or "0")[:6].ljust(6, "0")
    stamp = f"{match['base']}.{fraction}{'+00:00' if zone == 'Z' else zone}"
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None
