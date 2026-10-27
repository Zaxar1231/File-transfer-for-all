"""Queue behaviour: priorities, pause/resume, automatic retries with resume, journal."""

import threading

from fta import register
from fta.app.transfers import JobState, TransferJob, TransferQueue
from fta.core.journal import Journal
from fta.transports import memory


def job_for(source, destination, **kwargs):
    return TransferJob(
        name=str(source).rsplit("/", 1)[-1],
        source=str(source),
        destination=str(destination),
        source_url=str(source),
        destination_url=str(destination),
        **kwargs,
    )


class GateTransport(memory.MemoryTransport):
    """Uploads wait until the test opens the gate."""

    schemes = ("gate",)
    gate = threading.Event()

    def upload(self, local, remote, progress=None, offset=0):
        assert self.gate.wait(10)
        super().upload(local, remote, progress, offset)


class FlakyTransport(memory.MemoryTransport):
    """The first upload stores half the file and drops the connection."""

    schemes = ("flaky",)
    failures_left = 0

    def upload(self, local, remote, progress=None, offset=0):
        if FlakyTransport.failures_left:
            FlakyTransport.failures_left -= 1
            data = local.read_bytes()
            memory._stores[self._host][memory._norm(remote)] = data[: len(data) // 2]
            raise ConnectionResetError("connection dropped mid-transfer")
        super().upload(local, remote, progress, offset)


def test_higher_priority_starts_first(tmp_path):
    register(GateTransport)
    memory.reset()
    GateTransport.gate.clear()
    for name in ("gate", "low", "high"):
        (tmp_path / name).write_bytes(b"x")
    queue = TransferQueue(parallel=1, update_interval=0)
    started = []
    gate_running = threading.Event()

    def record(job):
        if job.state is JobState.RUNNING and job.name not in started:
            started.append(job.name)
            if job.name == "gate":
                gate_running.set()

    queue.subscribe(record)
    queue.add(job_for(tmp_path / "gate", "gate://box/"))  # holds the only slot
    assert gate_running.wait(10)
    queue.add(job_for(tmp_path / "low", "mem://box/", priority=0))
    queue.add(job_for(tmp_path / "high", "mem://box/", priority=5))
    GateTransport.gate.set()
    assert queue.wait_idle()

    assert started == ["gate", "high", "low"]


def test_pause_then_resume_sends_only_the_rest(tmp_path):
    payload = bytes(range(256)) * 120_000  # 30 MB, hundreds of progress reports
    src = tmp_path / "big.bin"
    src.write_bytes(payload)
    out = tmp_path / "out"
    out.mkdir()
    queue = TransferQueue(update_interval=0)
    paused = threading.Event()

    def pause_once(job):
        if job.state is JobState.RUNNING and job.bytes_done and not paused.is_set():
            paused.set()
            queue.pause(job)

    queue.subscribe(pause_once)
    job = queue.add(job_for(src, out))
    assert paused.wait(10)
    assert queue.wait_idle()
    assert job.state is JobState.PAUSED

    queue.resume(job)
    assert queue.wait_idle()

    assert job.state is JobState.DONE
    assert job.results[0].resumed_from > 0
    assert (out / "big.bin").read_bytes() == payload


def test_failed_attempt_is_retried_and_resumed(tmp_path):
    register(FlakyTransport)
    memory.reset()
    FlakyTransport.failures_left = 1
    payload = b"0123456789" * 50_000
    src = tmp_path / "a.bin"
    src.write_bytes(payload)
    queue = TransferQueue(update_interval=0, delay=lambda attempt: 0.05)

    job = queue.add(job_for(src, "flaky://box/a.bin", max_retries=2))
    assert queue.wait_idle()

    assert job.state is JobState.DONE
    assert job.attempts == 2
    assert job.results[0].resumed_from == len(payload) // 2
    assert memory._stores["box"]["/a.bin"] == payload


def test_errors_a_retry_cannot_fix_fail_at_once(tmp_path):
    queue = TransferQueue(update_interval=0, delay=lambda attempt: 0.05)
    job = queue.add(job_for(tmp_path / "missing.bin", tmp_path, max_retries=3))
    assert queue.wait_idle()
    assert job.state is JobState.FAILED
    assert job.attempts == 1


def test_journal_records_hashes_and_failures(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    out = tmp_path / "out"
    out.mkdir()
    journal = Journal(tmp_path / "journal.jsonl")
    queue = TransferQueue(update_interval=0, journal=journal)

    queue.add(job_for(src, out, verify=True))
    queue.add(job_for(tmp_path / "missing", out))
    assert queue.wait_idle()

    entries = {entry.status: entry for entry in journal.entries()}
    assert entries["done"].sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert entries["done"].verified
    assert "missing" in entries["failed"].source
