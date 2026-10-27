"""Copy files and directory trees between any pair of local paths and URLs."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from fta.core.endpoint import Endpoint
from fta.core.errors import TransferError
from fta.core.registry import open_transport
from fta.core.transport import ProgressCallback, Transport, join_path


def copy(
    source: str,
    destination: str,
    progress: ProgressCallback | None = None,
    *,
    recursive: bool = False,
    on_file: Callable[[str], None] | None = None,
    source_options: dict[str, Any] | None = None,
    destination_options: dict[str, Any] | None = None,
) -> None:
    """Copy a file, or a directory tree when ``recursive`` is set.

    Either side may be a local path or a URL. When the destination is an
    existing directory or ends with "/", the source is copied into it under its
    own name. Remote-to-remote copies are staged through a temporary local file,
    so a protocol only implements upload and download, never transfers to other
    protocols. ``on_file`` is called with each source file path before it is copied.
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
        copier = _Copier(src, dst, Path(staging), progress, on_file)
        target = _resolve_target(dst, dst_ep.path, name)
        if recursive and src.stat(src_ep.path).is_dir:
            copier.tree(src_ep.path, target)
        else:
            copier.file(src_ep.path, target)


class _Copier:
    def __init__(
        self,
        src: Transport,
        dst: Transport,
        staging: Path,
        progress: ProgressCallback | None,
        on_file: Callable[[str], None] | None,
    ) -> None:
        self.src = src
        self.dst = dst
        self.staging = staging
        self.progress = progress
        self.on_file = on_file

    def file(self, src_path: str, dst_path: str) -> None:
        if self.on_file:
            self.on_file(src_path)
        if self.src.endpoint.is_local:
            self.dst.upload(Path(src_path), dst_path, self.progress)
        elif self.dst.endpoint.is_local:
            self.src.download(src_path, Path(dst_path), self.progress)
        else:
            staged = self.staging / "staged"
            self.src.download(src_path, staged, self.progress)
            try:
                self.dst.upload(staged, dst_path, self.progress)
            finally:
                staged.unlink(missing_ok=True)

    def tree(self, src_path: str, dst_path: str) -> None:
        self.dst.mkdir(dst_path, exist_ok=True)
        for entry in self.src.listdir(src_path):
            child = join_path(dst_path, entry.name)
            if entry.is_dir:
                self.tree(entry.path, child)
            else:
                self.file(entry.path, child)


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
