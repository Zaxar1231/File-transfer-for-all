"""In-memory transport (mem://bucket/path): the "empty" protocol for tests and demos.

Every host name is a separate store shared by all connections in the process,
so data written through one transport is visible through the next.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path, PurePosixPath

from fta.core.errors import NotFound
from fta.core.registry import register
from fta.core.transport import CHUNK_SIZE, Progress, ProgressCallback, RemoteEntry, Transport, open_for_resume

_stores: dict[str, dict[str, bytes]] = {}
_dirs: dict[str, set[str]] = {}
_lock = threading.Lock()


def reset() -> None:
    """Forget everything stored in every mem:// host."""
    with _lock:
        _stores.clear()
        _dirs.clear()


@register
class MemoryTransport(Transport):
    schemes = ("mem",)
    can_resume_download = True
    can_resume_upload = True
    can_patch = True

    def connect(self) -> None:
        with _lock:
            _stores.setdefault(self._host, {})
            _dirs.setdefault(self._host, {"/"})

    def close(self) -> None:
        """Data outlives the connection on purpose."""

    @property
    def _host(self) -> str:
        return self.endpoint.host or ""

    @property
    def _files(self) -> dict[str, bytes]:
        return _stores[self._host]

    def upload(self, local: Path, remote: str, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        tracker = Progress(progress, local.stat().st_size, done=offset)
        chunks = [self._files.get(_norm(remote), b"")[:offset]]
        with local.open("rb") as fp:
            fp.seek(offset)
            for chunk in iter(lambda: fp.read(CHUNK_SIZE), b""):
                chunks.append(chunk)
                tracker.advance(len(chunk))
        with _lock:
            self._files[_norm(remote)] = b"".join(chunks)
            self._add_parents(_norm(remote))

    def patch(
        self, local: Path, remote: str, ranges: list[tuple[int, int]], size: int, progress: ProgressCallback | None = None
    ) -> None:
        tracker = Progress(progress, sum(end - start for start, end in ranges))
        data = bytearray(self._read(remote))
        with local.open("rb") as fp:
            for start, end in ranges:
                fp.seek(start)
                chunk = fp.read(end - start)
                data[start : start + len(chunk)] = chunk
                tracker.advance(len(chunk))
        with _lock:
            self._files[_norm(remote)] = bytes(data[:size])

    def download(self, remote: str, local: Path, progress: ProgressCallback | None = None, offset: int = 0) -> None:
        data = self._read(remote)
        tracker = Progress(progress, len(data), done=offset)
        with open_for_resume(local, offset) as out:
            for start in range(offset, len(data), CHUNK_SIZE):
                chunk = data[start : start + CHUNK_SIZE]
                out.write(chunk)
                tracker.advance(len(chunk))

    def listdir(self, remote: str) -> list[RemoteEntry]:
        base = _norm(remote)
        if base not in _dirs[self._host]:
            raise NotFound(f"{remote}: no such directory")
        prefix = base.rstrip("/") + "/"
        entries = {}
        for path in [*self._files, *_dirs[self._host]]:
            if path != base and path.startswith(prefix) and "/" not in path[len(prefix) :]:
                entries[path] = self.stat(path)
        return sorted(entries.values(), key=lambda entry: entry.name)

    def stat(self, remote: str) -> RemoteEntry:
        path = _norm(remote)
        name = PurePosixPath(path).name
        if path in self._files:
            return RemoteEntry(name, path, False, len(self._files[path]), time.time())
        if path in _dirs[self._host]:
            return RemoteEntry(name, path, True)
        raise NotFound(f"{remote}: no such file or directory")

    def mkdir(self, remote: str, exist_ok: bool = False) -> None:
        path = _norm(remote)
        if path in _dirs[self._host] and not exist_ok:
            raise FileExistsError(remote)
        with _lock:
            self._add_parents(path)
            _dirs[self._host].add(path)

    def delete(self, remote: str) -> None:
        path = _norm(remote)
        with _lock:
            if self._files.pop(path, None) is None:
                if path not in _dirs[self._host]:
                    raise NotFound(remote)
                _dirs[self._host].discard(path)

    def _read(self, remote: str) -> bytes:
        try:
            return self._files[_norm(remote)]
        except KeyError:
            raise NotFound(f"{remote}: no such file") from None

    def _add_parents(self, path: str) -> None:
        for parent in PurePosixPath(path).parents:
            _dirs[self._host].add(str(parent))


def _norm(path: str) -> str:
    return "/" + path.strip("/") if path.strip("/") else "/"
