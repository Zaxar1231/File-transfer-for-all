"""Local filesystem: plain paths and file:// URLs."""

from __future__ import annotations

from pathlib import Path

from fta.core.errors import NotFound, TransferError
from fta.core.registry import register
from fta.core.transport import Progress, ProgressCallback, RemoteEntry, Transport, copy_stream, open_for_resume


@register
class LocalTransport(Transport):
    schemes = ("", "file")
    can_resume_download = True
    can_resume_upload = True

    def connect(self) -> None:
        """Nothing to open."""

    def close(self) -> None:
        """Nothing to release."""

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        _copy_file(local, Path(remote), progress, offset)

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        _copy_file(Path(remote), local, progress, offset)

    def listdir(self, remote: str) -> list[RemoteEntry]:
        return [_entry(child) for child in sorted(Path(remote or ".").iterdir())]

    def stat(self, remote: str) -> RemoteEntry:
        path = Path(remote or ".")
        if not path.exists():
            raise NotFound(f"{remote}: no such file or directory")
        return _entry(path)

    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        Path(remote).mkdir(parents=True, exist_ok=exist_ok)

    def delete(self, remote: str) -> None:
        path = Path(remote)
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()


def _copy_file(source: Path, target: Path, progress: ProgressCallback | None, offset: int = 0) -> None:
    if target.exists() and source.samefile(target):
        raise TransferError(f"{source} and {target} are the same file")
    with source.open("rb") as reader, open_for_resume(target, offset) as writer:
        reader.seek(offset)
        copy_stream(reader, writer, Progress(progress, source.stat().st_size, done=offset))


def _entry(path: Path) -> RemoteEntry:
    info = path.stat()
    is_dir = path.is_dir()
    return RemoteEntry(
        name=path.name,
        path=str(path),
        is_dir=is_dir,
        size=None if is_dir else info.st_size,
        modified=info.st_mtime,
    )
