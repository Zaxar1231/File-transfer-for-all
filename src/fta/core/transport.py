"""The Transport interface every protocol implements, plus shared helpers."""

from __future__ import annotations

import functools
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, ClassVar, TypeVar

from fta.core.endpoint import Endpoint
from fta.core.errors import TransferError, UnsupportedOperation

CHUNK_SIZE = 64 * 1024

ProgressCallback = Callable[[int, int | None], None]
"""Receives ``(bytes_done, bytes_total)``; the total is ``None`` when unknown."""


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified: float | None = None
    """POSIX timestamp, if the protocol reports one."""


class Progress:
    """Counts bytes and forwards them to a ProgressCallback.

    Safe to advance from several threads, since some libraries (boto3) transfer
    parts in parallel.
    """

    def __init__(self, callback: ProgressCallback | None, total: int | None = None, done: int = 0) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self.total = total
        self.done = done
        """Starts above zero when a transfer resumes."""

    def advance(self, count: int) -> None:
        with self._lock:
            self.done += count
            if self._callback:
                self._callback(self.done, self.total)

    def wrap_reader(self, fp: BinaryIO) -> _ProgressReader:
        """Wrap a file so that every read done by a protocol library is counted."""
        return _ProgressReader(fp, self)


class _ProgressReader:
    def __init__(self, fp: BinaryIO, progress: Progress) -> None:
        self._fp = fp
        self._progress = progress

    def read(self, size: int = -1) -> bytes:
        chunk = self._fp.read(size)
        self._progress.advance(len(chunk))
        return chunk


def copy_stream(reader: Any, writer: BinaryIO, progress: Progress) -> None:
    for chunk in iter(lambda: reader.read(CHUNK_SIZE), b""):
        writer.write(chunk)
        progress.advance(len(chunk))


def join_path(parent: str, name: str) -> str:
    return f"{parent.rstrip('/')}/{name}"


def open_for_resume(path: Path, offset: int) -> BinaryIO:
    """Open a local file for writing at ``offset``: truncate to it, or start afresh at 0."""
    if offset:
        fp = path.open("r+b")
        fp.truncate(offset)
        fp.seek(offset)
        return fp
    return path.open("wb")


_Method = TypeVar("_Method", bound=Callable[..., Any])


def translate_errors(method: _Method) -> _Method:
    """Re-raise the protocol library's exceptions (``self.library_errors``) as TransferError."""

    @functools.wraps(method)
    def wrapper(self: Transport, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except self.library_errors as exc:
            raise TransferError(f"{self.endpoint.scheme}: {exc}") from exc

    return wrapper  # type: ignore[return-value]


class Transport(ABC):
    """Base class for one protocol.

    Subclasses set ``schemes`` and override the operations they support.
    Operations a protocol lacks keep raising ``UnsupportedOperation``.
    Paths given to operations are the path part of the URL, e.g. ``/dir/file``.
    """

    schemes: ClassVar[tuple[str, ...]] = ()

    library_errors: tuple[type[BaseException], ...] = ()
    """Exceptions of the underlying library that ``translate_errors`` converts."""

    can_resume_download: ClassVar[bool] = False
    can_resume_upload: ClassVar[bool] = False
    """Whether ``download``/``upload`` honour ``offset`` and continue a partial file."""

    def __init__(self, endpoint: Endpoint, **options: Any) -> None:
        self.endpoint = endpoint
        self.options = options

    @property
    def username(self) -> str | None:
        return self.options.get("username") or self.endpoint.username

    @property
    def password(self) -> str | None:
        """Prefer the ``password`` option: a password in the URL ends up in shell history."""
        return self.options.get("password") or self.endpoint.password

    def float_option(self, name: str, default: float) -> float:
        return float(self.options.get(name, default))

    def bool_option(self, name: str, default: bool = False) -> bool:
        value = self.options.get(name, default)
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")

    @abstractmethod
    def connect(self) -> None:
        """Open the session (handshake, login). Stateless protocols may do nothing."""

    @abstractmethod
    def close(self) -> None:
        """Release the session. Must be safe to call after a failed connect."""

    def __enter__(self) -> Transport:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # The roadmap's send/recv/list/resume map to upload/download/listdir and the
    # ``offset`` argument: with offset > 0 the first ``offset`` bytes are already at
    # the destination and the transfer continues from there.

    def upload(
        self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0
    ) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot upload")

    def download(
        self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0
    ) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot download")

    def listdir(self, remote: str) -> list[RemoteEntry]:
        raise UnsupportedOperation(f"{type(self).__name__} cannot list directories")

    def stat(self, remote: str) -> RemoteEntry:
        """Describe one path; raise NotFound if it does not exist."""
        raise UnsupportedOperation(f"{type(self).__name__} cannot stat paths")

    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot create directories")

    def delete(self, remote: str) -> None:
        """Delete a file or an empty directory."""
        raise UnsupportedOperation(f"{type(self).__name__} cannot delete")
