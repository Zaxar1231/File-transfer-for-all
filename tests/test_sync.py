"""Folder sync with delta transfer, and the shared speed limit."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from fta.core.integrity import BLOCK_SIZE
from fta.core.sync import changed_ranges, sync
from fta.core.throttle import LIMITER, Limiter, Metered, parse_rate
from fta.transports import memory
from test_protocols import sftp_url  # noqa: F401 - fixture


def make_source(root):
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.txt").write_text("alpha")
    big = root / "video.bin"
    big.write_bytes(os.urandom(int(2.5 * BLOCK_SIZE)))  # three blocks
    (root / "empty").mkdir()
    return big


@pytest.fixture(params=["local", "memory"])
def destination(request, tmp_path):
    if request.param == "local":
        return str(tmp_path / "mirror")
    memory.reset()
    return "mem://backup/mirror"


def read(destination: str, rel: str) -> bytes:
    if destination.startswith("mem://"):
        return memory._stores["backup"][f"/mirror/{rel}"]
    return (Path(destination) / rel).read_bytes()


def test_sync_copies_then_skips_then_patches_only_changed_blocks(tmp_path, destination):
    source = tmp_path / "src"
    big = make_source(source)
    manifest = tmp_path / "manifest.json"

    first = sync(str(source), destination, manifest=manifest)
    assert sorted(first.copied) == ["docs/a.txt", "video.bin"] and not first.patched
    assert read(destination, "video.bin") == big.read_bytes()

    again = sync(str(source), destination, manifest=manifest)
    assert again.copied == [] and again.patched == [] and again.bytes_sent == 0
    assert sorted(again.skipped) == ["docs/a.txt", "video.bin"]

    # Change a few bytes in the middle block only.
    data = bytearray(big.read_bytes())
    data[BLOCK_SIZE + 100 : BLOCK_SIZE + 110] = b"0123456789"
    big.write_bytes(bytes(data))
    os.utime(big, (time.time() + 5, time.time() + 5))
    delta = sync(str(source), destination, manifest=manifest)
    assert delta.patched == ["video.bin"]
    assert delta.bytes_sent == BLOCK_SIZE and delta.bytes_saved == len(data) - BLOCK_SIZE
    assert read(destination, "video.bin") == bytes(data)

    # Shrinking and growing work through the same path.
    big.write_bytes(bytes(data[: BLOCK_SIZE + 5]))
    os.utime(big, (time.time() + 10, time.time() + 10))
    assert sync(str(source), destination, manifest=manifest).patched == ["video.bin"]
    assert read(destination, "video.bin") == bytes(data[: BLOCK_SIZE + 5])


def test_a_copy_changed_by_someone_else_is_sent_whole(tmp_path):
    source, mirror = tmp_path / "src", tmp_path / "mirror"
    big = make_source(source)
    manifest = tmp_path / "manifest.json"
    sync(str(source), str(mirror), manifest=manifest)

    # Someone edits the copy (same size), and the source changes too.
    copy = mirror / "video.bin"
    tampered = bytearray(copy.read_bytes())
    tampered[0:5] = b"XXXXX"
    copy.write_bytes(bytes(tampered))
    os.utime(copy, (time.time() + 60, time.time() + 60))
    data = bytearray(big.read_bytes())
    data[-3:] = b"end"
    big.write_bytes(bytes(data))
    os.utime(big, (time.time() + 90, time.time() + 90))

    report = sync(str(source), str(mirror), manifest=manifest)
    assert report.copied == ["video.bin"] and report.patched == []
    assert copy.read_bytes() == bytes(data)


def test_delete_mirrors_removals(tmp_path):
    source, mirror = tmp_path / "src", tmp_path / "mirror"
    make_source(source)
    sync(str(source), str(mirror))
    (source / "docs" / "a.txt").unlink()
    keep = sync(str(source), str(mirror))
    assert (mirror / "docs" / "a.txt").exists() and keep.deleted == []
    gone = sync(str(source), str(mirror), delete=True)
    assert "docs/a.txt" in gone.deleted and not (mirror / "docs" / "a.txt").exists()


def test_sync_over_sftp_patches_in_place(tmp_path, sftp_url):  # noqa: F811 - pytest fixture
    url, options = sftp_url, {"password": "secret", "accept_unknown_host": "true"}
    source = tmp_path / "src"
    big = make_source(source)
    manifest = tmp_path / "manifest.json"
    sync(str(source), url + "/mirror", manifest=manifest, destination_options=options)
    data = bytearray(big.read_bytes())
    data[-10:] = b"9876543210"
    big.write_bytes(bytes(data))
    os.utime(big, (time.time() + 5, time.time() + 5))
    report = sync(str(source), url + "/mirror", manifest=manifest, destination_options=options)
    assert report.patched == ["video.bin"] and report.bytes_sent < len(data) / 2

    from fta.core.registry import open_transport

    out = tmp_path / "back.bin"
    with open_transport(url, **options) as transport:
        transport.download(transport.endpoint.path.rstrip("/") + "/mirror/video.bin", out)
    assert out.read_bytes() == bytes(data)


def test_changed_ranges_merges_neighbours():
    from fta.core.integrity import Digest

    new = Digest("x", 10 * 4, ("a", "B", "C", "d", "E"))
    assert changed_ranges(["a", "b", "c", "d"], new, block_size=10) == [(10, 30), (40, 40)]


def test_speed_limit(tmp_path):
    assert parse_rate("500K") == 512_000 and parse_rate("2M") == 2 * 1024**2 and parse_rate("0") == 0
    limiter = Limiter(1024 * 1024)  # 1 MiB/s
    meter = Metered(None, limiter)
    started = time.monotonic()
    for done in range(0, 1024 * 1024 + 1, 64 * 1024):
        meter(done, None)
    elapsed = time.monotonic() - started
    assert 0.6 < elapsed < 2.0  # the first quarter second rides on the burst

    # The process-wide limiter slows a real copy.
    from fta.core.manager import copy

    payload = tmp_path / "p.bin"
    payload.write_bytes(os.urandom(512 * 1024))
    LIMITER.set_rate(1024 * 1024)
    try:
        started = time.monotonic()
        copy(str(payload), str(tmp_path / "out.bin"))
        assert time.monotonic() - started > 0.2
    finally:
        LIMITER.set_rate(0)


def test_schedules_run_at_their_time_once(tmp_path):
    import datetime

    from fta.app.scheduler import Scheduler, describe
    from fta.app.storage import Schedule, Store
    from fta.app.transfers import JobState, TransferQueue

    store = Store(tmp_path / "config.toml", environ={})
    source, mirror = tmp_path / "src", tmp_path / "mirror"
    make_source(source)
    store.schedules.append(Schedule("Backup", str(source), str(mirror), time="03:00", days=[0, 1, 2, 3, 4]))
    store.save()
    queue = TransferQueue(update_interval=0)
    scheduler = Scheduler(store, queue)

    monday_2am = datetime.datetime(2026, 10, 26, 2, 0).timestamp()
    monday_4am = datetime.datetime(2026, 10, 26, 4, 0).timestamp()
    saturday_4am = datetime.datetime(2026, 10, 31, 4, 0).timestamp()
    assert scheduler.tick(monday_2am) == []  # not yet
    assert [s.name for s in scheduler.tick(monday_4am)] == ["Backup"]  # due (or missed while closed)
    assert scheduler.tick(monday_4am + 600) == []  # only once a day
    assert queue.wait_idle(30)
    job = queue.jobs[0]
    assert job.state is JobState.DONE, job.error
    assert (mirror / "video.bin").exists() and {r.destination for r in job.results} == {"/docs/a.txt", "/video.bin"}
    assert Store(tmp_path / "config.toml", environ={}).schedules[0].last_run == monday_4am  # survives a restart
    assert scheduler.tick(saturday_4am) == []  # not on weekends
    assert describe(store.schedules[0]) == "синхронизация · пн, вт, ср, чт, пт в 03:00"
