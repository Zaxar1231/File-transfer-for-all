"""SHA-256 of whole files and of fixed-size blocks.

The whole-file hash proves a copy is identical; block hashes say *where* two
files differ, which delta sync uses to send only changed blocks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

BLOCK_SIZE = 4 * 1024 * 1024
_READ_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Digest:
    sha256: str
    size: int
    blocks: tuple[str, ...]
    """SHA-256 of each ``block_size`` block; the last block may be shorter."""

    def first_difference(self, other: Digest) -> int | None:
        """Index of the first block that differs, or None when the files are identical."""
        if self.sha256 == other.sha256 and self.size == other.size:
            return None
        for index, (mine, theirs) in enumerate(zip(self.blocks, other.blocks)):
            if mine != theirs:
                return index
        return min(len(self.blocks), len(other.blocks))


class Hasher:
    """Feeds bytes into the whole-file hash and the per-block hashes at once."""

    def __init__(self, block_size: int = BLOCK_SIZE) -> None:
        self.block_size = block_size
        self.size = 0
        self._total = hashlib.sha256()
        self._block = hashlib.sha256()
        self._in_block = 0
        self._blocks: list[str] = []

    def update(self, data: bytes) -> None:
        self._total.update(data)
        self.size += len(data)
        view = memoryview(data)
        while view:
            part = view[: self.block_size - self._in_block]
            self._block.update(part)
            self._in_block += len(part)
            view = view[len(part) :]
            if self._in_block == self.block_size:
                self._blocks.append(self._block.hexdigest())
                self._block = hashlib.sha256()
                self._in_block = 0

    def digest(self) -> Digest:
        blocks = list(self._blocks)
        if self._in_block:
            blocks.append(self._block.hexdigest())
        return Digest(self._total.hexdigest(), self.size, tuple(blocks))


def file_digest(path: Path, block_size: int = BLOCK_SIZE) -> Digest:
    hasher = Hasher(block_size)
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(_READ_SIZE), b""):
            hasher.update(chunk)
    return hasher.digest()
