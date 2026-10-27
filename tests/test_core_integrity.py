"""SHA-256 digests, verification of copies, and the transfer journal."""

import csv
import hashlib

import pytest

from fta import IntegrityError, copy, register
from fta.core.integrity import Hasher, file_digest
from fta.core.journal import Journal, JournalEntry
from fta.transports import memory


def test_block_digests_match_hashlib(tmp_path):
    data = bytes(range(256)) * 50_000  # 12.8 MB: three 4 MiB blocks and a tail
    path = tmp_path / "f.bin"
    path.write_bytes(data)

    digest = file_digest(path)

    assert digest.sha256 == hashlib.sha256(data).hexdigest()
    assert digest.size == len(data)
    block = 4 * 1024 * 1024
    assert list(digest.blocks) == [hashlib.sha256(data[i : i + block]).hexdigest() for i in range(0, len(data), block)]


def test_hasher_is_independent_of_chunking():
    data = b"abcdefghij" * 1000
    one, many = Hasher(block_size=64), Hasher(block_size=64)
    one.update(data)
    for start in range(0, len(data), 7):
        many.update(data[start : start + 7])
    assert one.digest() == many.digest()


def test_first_difference_names_the_changed_block():
    base = b"x" * 300
    changed = base[:150] + b"y" + base[151:]
    a, b = Hasher(block_size=100), Hasher(block_size=100)
    a.update(base)
    b.update(changed)
    assert a.digest().first_difference(a.digest()) is None
    assert a.digest().first_difference(b.digest()) == 1


class CorruptingTransport(memory.MemoryTransport):
    """Stores what it is given but flips one byte: a lying or broken server."""

    schemes = ("corrupt",)

    def upload(self, local, remote, progress=None, offset=0):
        super().upload(local, remote, progress, offset)
        stored = bytearray(memory._stores[self._host][memory._norm(remote)])
        stored[len(stored) // 2] ^= 0xFF
        memory._stores[self._host][memory._norm(remote)] = bytes(stored)


def test_verify_catches_a_corrupted_copy(tmp_path):
    register(CorruptingTransport)
    memory.reset()
    src = tmp_path / "a.bin"
    src.write_bytes(b"payload" * 10_000)

    [unverified] = copy(str(src), "corrupt://box/a.bin", checksum=True)
    assert unverified.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert not unverified.verified

    with pytest.raises(IntegrityError, match="block #0"):
        copy(str(src), "corrupt://box/a.bin", verify=True)


def test_journal_roundtrip_and_csv(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl")
    journal.append(JournalEntry("a", "b", 10, "done", sha256="ab" * 32, time=1_700_000_000))
    with journal.path.open("a") as fp:
        fp.write("{broken line\n")  # a crash mid-write must not lose the rest
    journal.append(JournalEntry("c", "d", 0, "failed", error="boom", time=1_700_000_100))

    entries = journal.entries()
    assert [e.source for e in entries] == ["c", "a"]  # newest first
    assert journal.entries(limit=1)[0].error == "boom"

    rows = list(csv.DictReader(journal.export_csv(tmp_path / "out.csv").open(encoding="utf-8")))
    assert [row["source"] for row in rows] == ["a", "c"]  # chronological in the export
    assert rows[0]["sha256"] == "ab" * 32
