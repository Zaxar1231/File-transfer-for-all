"""Copy files and directory trees between any pair of local paths and URLs."""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from fta.core.endpoint import Endpoint
from fta.core.errors import IntegrityError, TransferError
from fta.core.integrity import Digest, file_digest
from fta.core.registry import open_transport
from fta.core.transport import ProgressCallback, Transport, join_path


@dataclass(frozen=True)
class FileResult:
    source: str
    destination: str
    size: int
    seconds: float
    sha256: str = ""
    """Filled when checksums or verification were asked for."""
    verified: bool = False
    resumed_from: int = 0
    """Bytes that were already at the destination and were not sent again."""


def copy(
    source: str,
    destination: str,
    progress: ProgressCallback | None = None,
    *,
    recursive: bool = False,
    resume: bool = False,
    checksum: bool = False,
    verify: bool = False,
    on_file: Callable[[str], None] | None = None,
    source_options: dict[str, Any] | None = None,
    destination_options: dict[str, Any] | None = None,
) -> list[FileResult]:
    """Copy a file, or a directory tree when ``recursive`` is set; return one result per file.

    Either side may be a local path or a URL. When the destination is an
    existing directory or ends with "/", the source is copied into it under its
    own name. Remote-to-remote copies are staged through a temporary local file,
    so a protocol only implements upload and download, never transfers to other
    protocols.

    ``resume`` continues partial files where the protocol allows it. ``checksum``
    records each file's SHA-256; ``verify`` also hashes the other side and raises
    IntegrityError when they differ. ``on_file`` is called with each source path.
    """
    src_ep = Endpoint.parse(source)
    dst_ep = Endpoint.parse(destination)
    name = _basename(src_ep)
    if not name:
        raise TransferError(f"no file name in {source!r}")

    with (
        open_transport(source, **(source_options or {})) as src,
        open_transport(destination, **(destination_options or {})) as dst,
        tempfile.TemporaryDirectory(prefix="fta-") as staging,
    ):
        copier = _Copier(src, dst, Path(staging), progress, on_file, resume, checksum or verify, verify)
        target = _resolve_target(dst, dst_ep.path, name)
        if recursive and src.stat(src_ep.path).is_dir:
            copier.tree(src_ep.path, target)
        else:
            copier.file(src_ep.path, target)
        return copier.results


class _Copier:
    def __init__(
        self,
        src: Transport,
        dst: Transport,
        staging: Path,
        progress: ProgressCallback | None,
        on_file: Callable[[str], None] | None,
        resume: bool,
        checksum: bool,
        verify: bool,
    ) -> None:
        self.src = src
        self.dst = dst
        self.staging = staging
        self.progress = progress
        self.on_file = on_file
        self.resume = resume
        self.checksum = checksum
        self.verify = verify
        self.results: list[FileResult] = []

    def file(self, src_path: str, dst_path: str) -> None:
        if self.on_file:
            self.on_file(src_path)
        started = time.monotonic()
        offset = 0
        if self.src.endpoint.is_local:
            local = Path(src_path)
            size = local.stat().st_size
            if self.resume and self.dst.can_resume_upload:
                offset = _existing_size(self.dst, dst_path, size)
            if offset < size or size == 0:
                _send(self.dst, local, dst_path, self.progress, offset)
            mine = local
        elif self.dst.endpoint.is_local:
            target = Path(dst_path)
            full_size = None
            if self.resume and self.src.can_resume_download and target.is_file():
                full_size = self.src.stat(src_path).size
                offset = _existing_size_local(target, full_size)
            if not (offset and offset == full_size):  # a complete file needs no request at all
                _recv(self.src, src_path, target, self.progress, offset)
            mine = target
        else:
            mine = self.staging / "staged"
            self.src.download(src_path, mine, self.progress)
            try:
                self.dst.upload(mine, dst_path, self.progress)
                result = self._finish(src_path, dst_path, mine, started, 0)
            finally:
                mine.unlink(missing_ok=True)
            self.results.append(result)
            return
        self.results.append(self._finish(src_path, dst_path, mine, started, offset))

    def _finish(self, src_path: str, dst_path: str, local: Path, started: float, offset: int) -> FileResult:
        digest = file_digest(local) if self.checksum else None
        verified = False
        if self.verify and digest is not None:
            # ``local`` is one side (source, destination or the staged copy); hash the other.
            other_side, other_path = (self.src, src_path) if self.dst.endpoint.is_local else (self.dst, dst_path)
            other = self._remote_digest(other_side, other_path)
            block = digest.first_difference(other)
            if block is not None:
                raise IntegrityError(
                    f"SHA-256 mismatch for {dst_path}: {digest.sha256[:12]} ≠ {other.sha256[:12]}, "
                    f"first differing 4 MiB block #{block}"
                )
            verified = True
        return FileResult(
            source=src_path,
            destination=dst_path,
            size=local.stat().st_size,
            seconds=time.monotonic() - started,
            sha256=digest.sha256 if digest else "",
            verified=verified,
            resumed_from=offset,
        )

    def _remote_digest(self, transport: Transport, path: str) -> Digest:
        if transport.endpoint.is_local:
            return file_digest(Path(path))
        probe = self.staging / "verify"
        try:
            transport.download(path, probe)
            return file_digest(probe)
        finally:
            probe.unlink(missing_ok=True)

    def tree(self, src_path: str, dst_path: str) -> None:
        self.dst.mkdir(dst_path, exist_ok=True)
        for entry in self.src.listdir(src_path):
            child = join_path(dst_path, entry.name)
            if entry.is_dir:
                self.tree(entry.path, child)
            else:
                self.file(entry.path, child)


def _send(transport: Transport, local: Path, remote: str, progress: ProgressCallback | None, offset: int) -> None:
    # ``offset`` only when resuming, so plugins written before resume support keep working
    if offset:
        transport.upload(local, remote, progress, offset=offset)
    else:
        transport.upload(local, remote, progress)


def _recv(transport: Transport, remote: str, local: Path, progress: ProgressCallback | None, offset: int) -> None:
    if offset:
        transport.download(remote, local, progress, offset=offset)
    else:
        transport.download(remote, local, progress)


def _existing_size(transport: Transport, path: str, full_size: int) -> int:
    """Bytes of ``path`` already at the destination; 0 when absent or larger than the source."""
    try:
        size = transport.stat(path).size or 0
    except (TransferError, OSError):
        return 0
    return size if size <= full_size else 0


def _existing_size_local(path: Path, full_size: int | None) -> int:
    size = path.stat().st_size
    return size if full_size is not None and size <= full_size else 0


def _basename(endpoint: Endpoint) -> str:
    if endpoint.is_local:
        return Path(endpoint.path).name
    return PurePosixPath(endpoint.path.rstrip("/")).name


def _resolve_target(dst: Transport, path: str, name: str) -> str:
    """Put the source inside ``path`` when it names a directory, else use ``path`` as is."""
    if not path or path.endswith("/"):
        return join_path(path or "/", name)
    try:
        if dst.stat(path).is_dir:
            return join_path(path, name)
    except (TransferError, OSError):
        pass  # missing, or the protocol cannot tell: treat it as the target file
    return path
