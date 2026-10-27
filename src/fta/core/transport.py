"""The Transport interface every protocol implements, plus shared progress helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, ClassVar

from fta.core.endpoint import Endpoint
from fta.core.errors import UnsupportedOperation

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
    """Counts bytes and forwards them to a ProgressCallback."""

    def __init__(self, callback: ProgressCallback | None, total: int | None = None) -> None:
        self._callback = callback
        self.total = total
        self.done = 0

    def advance(self, count: int) -> None:
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


class Transport(ABC):
    """Base class for one protocol.

    Subclasses set ``schemes`` and override the operations they support.
    Operations a protocol lacks keep raising ``UnsupportedOperation``.
    """

    schemes: ClassVar[tuple[str, ...]] = ()

    def __init__(self, endpoint: Endpoint, **options: Any) -> None:
        self.endpoint = endpoint
        self.options = options

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

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot upload")

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot download")

    def listdir(self, remote: str) -> list[RemoteEntry]:
        raise UnsupportedOperation(f"{type(self).__name__} cannot list directories")

    def mkdir(self, remote: str) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot create directories")

    def delete(self, remote: str) -> None:
        raise UnsupportedOperation(f"{type(self).__name__} cannot delete files")
