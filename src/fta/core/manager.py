"""Copy between any pair of local paths and remote URLs."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fta.core.endpoint import Endpoint
from fta.core.errors import TransferError
from fta.core.registry import open_transport
from fta.core.transport import ProgressCallback


def copy(
    source: str,
    destination: str,
    progress: ProgressCallback | None = None,
    *,
    source_options: dict[str, Any] | None = None,
    destination_options: dict[str, Any] | None = None,
) -> None:
    """Copy one file. Either side may be a local path or a URL.

    Remote-to-remote copies are staged through a temporary local file, so a
    protocol only has to implement upload and download, never transfers to
    other protocols. Directories are not supported yet.
    """
    src = Endpoint.parse(source)
    dst = Endpoint.parse(destination)
    name = Path(src.path).name
    if not name:
        raise TransferError(f"no file name in {source!r}")

    with tempfile.TemporaryDirectory(prefix="fta-") as staging:
        if src.is_local:
            staged = Path(src.path)
        else:
            staged = Path(staging) / name
            with open_transport(source, **(source_options or {})) as transport:
                transport.download(src.path, staged, progress)

        if dst.is_local:
            target = Path(dst.path)
            shutil.copyfile(staged, target / name if target.is_dir() else target)
        else:
            with open_transport(destination, **(destination_options or {})) as transport:
                transport.upload(staged, dst.path, progress)
