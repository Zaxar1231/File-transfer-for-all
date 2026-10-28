"""One-way folder sync with delta transfer.

``sync(source, destination)`` makes the destination folder hold what the source
holds. Unchanged files are skipped (size and time, as rsync's quick check). A
changed local file whose copy at the destination is untouched since our last
sync is *patched*: the manifest remembers the SHA-256 of every 4 MiB block we
sent, so only blocks that differ travel. Everything else is sent whole.

The manifest is a small JSON file kept by the app (the destination needs no
helper program, unlike rsync).
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fta.core.endpoint import Endpoint
from fta.core.errors import NotFound
from fta.core.integrity import BLOCK_SIZE, Digest, file_digest
from fta.core.registry import open_transport
from fta.core.throttle import Metered
from fta.core.transport import ProgressCallback, RemoteEntry, Transport, join_path

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    copied: list[str] = field(default_factory=list)
    patched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    bytes_sent: int = 0
    bytes_saved: int = 0
    """Bytes delta transfer did not have to send."""
    sent: list[tuple[str, int, int, str]] = field(default_factory=list)
    """(relative path, file size, bytes sent, sha256) for every copied or patched file."""

    def summary(self) -> str:
        return (
            f"{len(self.copied)} copied, {len(self.patched)} patched, {len(self.skipped)} unchanged, "
            f"{len(self.deleted)} deleted; {self.bytes_sent} bytes sent, {self.bytes_saved} saved by delta"
        )


class Manifest:
    """What we last wrote to each destination file: source size and time, block hashes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.entries: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("sync manifest %s is unreadable; starting afresh", path)

    def get(self, key: str) -> dict[str, Any] | None:
        return self.entries.get(key)

    def put(self, key: str, size: int, mtime: float | None, digest: Digest | None, written: float | None) -> None:
        """``mtime`` is the source file's; ``written`` the destination copy's after we wrote it."""
        with self._lock:
            self.entries[key] = {
                "size": size,
                "mtime": mtime,
                "written": written,
                "sha256": digest.sha256 if digest else "",
                "blocks": list(digest.blocks) if digest else [],
            }

    def drop(self, key: str) -> None:
        with self._lock:
            self.entries.pop(key, None)

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries), encoding="utf-8")
            tmp.replace(self.path)


def changed_ranges(old_blocks: list[str], new: Digest, block_size: int = BLOCK_SIZE) -> list[tuple[int, int]]:
    """Byte ranges of ``new`` whose blocks differ from ``old_blocks``, adjacent ones merged."""
    ranges: list[tuple[int, int]] = []
    for index, block in enumerate(new.blocks):
        if index < len(old_blocks) and old_blocks[index] == block:
            continue
        start, end = index * block_size, min((index + 1) * block_size, new.size)
        if ranges and ranges[-1][1] == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


def sync(
    source: str,
    destination: str,
    *,
    manifest: Path | None = None,
    delete: bool = False,
    progress: ProgressCallback | None = None,
    on_file: Callable[[str], None] | None = None,
    source_options: dict[str, Any] | None = None,
    destination_options: dict[str, Any] | None = None,
) -> SyncReport:
    """Mirror the folder ``source`` into the folder ``destination``; ``delete`` also removes
    destination files that are no longer in the source."""
    src_ep, dst_ep = Endpoint.parse(source), Endpoint.parse(destination)
    report = SyncReport()
    book = Manifest(manifest)
    metered = Metered(progress)
    key_base = _key_base(destination)
    with (
        open_transport(source, **(source_options or {})) as src,
        open_transport(destination, **(destination_options or {})) as dst,
        tempfile.TemporaryDirectory(prefix="fta-sync-") as staging,
    ):
        src_root, dst_root = src_ep.path.rstrip("/") or "/", dst_ep.path.rstrip("/") or "/"
        if not src.stat(src_root).is_dir:
            raise NotFound(f"{source} is not a folder")
        _ensure_dir(dst, dst_root)
        wanted = _walk(src, src_root)
        present = _walk(dst, dst_root) if _exists(dst, dst_root) else {}
        try:
            for rel, entry in sorted(wanted.items()):
                target = _join(dst_root, rel)
                if entry.is_dir:
                    if rel not in present:
                        _ensure_dir(dst, target)
                    continue
                if on_file:
                    on_file(rel)
                _sync_file(src, dst, entry, target, rel, present.get(rel), book, f"{key_base}{rel}", report, metered, Path(staging))
            if delete:
                for rel in sorted(set(present) - set(wanted), key=lambda r: -r.count("/")):
                    try:
                        dst.delete(_join(dst_root, rel))
                    except Exception as exc:  # noqa: BLE001 - a non-empty folder of someone else's files stays
                        logger.info("sync: kept %s: %s", rel, exc)
                        continue
                    book.drop(f"{key_base}{rel}")
                    report.deleted.append(rel)
        finally:
            book.save()
    return report


def _sync_file(
    src: Transport,
    dst: Transport,
    entry: RemoteEntry,
    target: str,
    rel: str,
    there: RemoteEntry | None,
    book: Manifest,
    key: str,
    report: SyncReport,
    progress: ProgressCallback,
    staging: Path,
) -> None:
    size, mtime = entry.size or 0, entry.modified
    previous = book.get(key)
    # Patching is only safe if nobody else wrote the copy since we did: same size and same time.
    untouched = (
        there is not None
        and previous is not None
        and there.size == previous["size"]
        and (previous.get("written") is None or there.modified is None or abs(there.modified - previous["written"]) < 1)
    )
    if untouched and previous["mtime"] == mtime and previous["size"] == size:
        report.skipped.append(rel)
        return
    if there is not None and previous is None and there.size == size and mtime and there.modified and there.modified >= mtime:
        # Same size and the copy is newer: the usual quick check says "same file". Remember it.
        local = Path(entry.path) if src.endpoint.is_local else None
        book.put(key, size, mtime, file_digest(local) if local else None, there.modified)
        report.skipped.append(rel)
        return

    local = Path(entry.path) if src.endpoint.is_local else _stage(src, entry.path, staging, progress)
    digest = file_digest(local)
    if untouched and dst.can_patch and previous.get("blocks"):
        ranges = changed_ranges(previous["blocks"], digest)
        sent = sum(end - start for start, end in ranges)
        dst.patch(local, target, ranges, digest.size, progress)
        report.patched.append(rel)
        report.bytes_sent += sent
        report.bytes_saved += digest.size - sent
        report.sent.append((rel, digest.size, sent, digest.sha256))
    else:
        dst.upload(local, target, progress)
        report.copied.append(rel)
        report.bytes_sent += digest.size
        report.sent.append((rel, digest.size, digest.size, digest.sha256))
    book.put(key, digest.size, mtime, digest, _modified(dst, target))


def _modified(transport: Transport, path: str) -> float | None:
    try:
        return transport.stat(path).modified
    except Exception:  # noqa: BLE001 - only a safety check is lost
        return None


def _stage(src: Transport, path: str, staging: Path, progress: ProgressCallback) -> Path:
    local = staging / f"{time.monotonic_ns()}"
    src.download(path, local, progress)
    return local


def _walk(transport: Transport, root: str) -> dict[str, RemoteEntry]:
    """Every file and folder under ``root``, keyed by POSIX path relative to it."""
    found: dict[str, RemoteEntry] = {}
    pending = [("", root)]
    while pending:
        rel_dir, path = pending.pop()
        for entry in transport.listdir(path):
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            found[rel] = entry
            if entry.is_dir:
                pending.append((rel, entry.path))
    return found


def _exists(transport: Transport, path: str) -> bool:
    try:
        transport.stat(path)
        return True
    except NotFound:
        return False
    except FileNotFoundError:
        return False


def _ensure_dir(transport: Transport, path: str) -> None:
    if path in ("", "/") or _exists(transport, path):
        return
    parent = path.rsplit("/", 1)[0]
    if parent and parent != path:
        _ensure_dir(transport, parent)
    transport.mkdir(path, exist_ok=True)


def _join(root: str, rel: str) -> str:
    return join_path(root, rel) if root != "/" else "/" + rel


def _key_base(destination: str) -> str:
    """The destination without credentials, so a changed password keeps the manifest valid."""
    ep = Endpoint.parse(destination)
    if ep.is_local:
        return str(Path(ep.path).resolve()) + "|"
    return f"{ep.scheme}://{ep.username + '@' if ep.username else ''}{ep.netloc}{ep.path.rstrip('/')}|"
