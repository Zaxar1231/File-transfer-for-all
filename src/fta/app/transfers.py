"""Background transfer queue: jobs run on worker threads, listeners get throttled updates."""

from __future__ import annotations

import enum
import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fta.core.manager import copy

logger = logging.getLogger(__name__)

class JobState(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class _Cancelled(Exception):
    """Raised from the progress callback to abort a running transfer."""


_ids = itertools.count(1)


@dataclass(eq=False)
class TransferJob:
    name: str
    source: str
    destination: str
    """Display labels for where the data comes from and goes to."""
    source_url: str
    destination_url: str
    recursive: bool = False
    source_options: dict[str, Any] = field(default_factory=dict)
    destination_options: dict[str, Any] = field(default_factory=dict)

    id: int = field(default_factory=lambda: next(_ids))
    state: JobState = JobState.QUEUED
    current_file: str = ""
    files_done: int = 0
    bytes_done: int = 0
    bytes_total: int | None = None
    speed: float = 0.0
    """Bytes per second for the current file."""
    error: str = ""

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _file_started: float = field(default=0.0, repr=False)
    _dirty: bool = field(default=False, repr=False)
    """Progress changed since listeners last heard about it."""

    @property
    def fraction(self) -> float | None:
        if self.state is JobState.DONE:
            return 1.0
        if self.bytes_total:
            return min(self.bytes_done / self.bytes_total, 1.0)
        return None

    def cancel(self) -> None:
        self._cancel.set()


Listener = Callable[[TransferJob], None]


class TransferQueue:
    """Runs jobs with at most ``parallel`` at a time.

    Listeners are called from worker threads on every state change and at most
    ``update_interval`` seconds apart while bytes flow. Progress that arrives in
    a burst and then stalls (a fast write into socket buffers, then a long wait
    for the server) is still delivered by the ticker, so views never stay stale.
    """

    def __init__(self, parallel: int = 2, update_interval: float = 0.15) -> None:
        self.jobs: list[TransferJob] = []
        self.parallel = parallel
        self.update_interval = update_interval
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._running = 0
        self._dispatcher = threading.Thread(target=self._dispatch, name="fta-dispatch", daemon=True)
        self._dispatcher.start()
        self._ticker = threading.Thread(target=self._tick, name="fta-progress", daemon=True)
        self._ticker.start()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def set_parallel(self, parallel: int) -> None:
        with self._lock:
            self.parallel = max(1, parallel)
            self._wakeup.notify()

    def add(self, job: TransferJob) -> TransferJob:
        with self._lock:
            self.jobs.append(job)
            self._wakeup.notify()
        self._notify(job)
        return job

    def retry(self, job: TransferJob) -> TransferJob:
        """Queue a fresh copy of a failed or cancelled job."""
        return self.add(
            TransferJob(
                name=job.name,
                source=job.source,
                destination=job.destination,
                source_url=job.source_url,
                destination_url=job.destination_url,
                recursive=job.recursive,
                source_options=job.source_options,
                destination_options=job.destination_options,
            )
        )

    def cancel(self, job: TransferJob) -> None:
        job.cancel()
        with self._lock:
            if job.state is JobState.QUEUED:
                job.state = JobState.CANCELLED
        self._notify(job)

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [job for job in self.jobs if not job.state.finished]

    @property
    def active_count(self) -> int:
        return sum(1 for job in self.jobs if not job.state.finished)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until no job is queued or running; for tests and shutdown."""
        deadline = time.monotonic() + timeout
        while self.active_count:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def _dispatch(self) -> None:
        while True:
            with self._lock:
                job = None
                while job is None:
                    if self._running < self.parallel:
                        job = next((j for j in self.jobs if j.state is JobState.QUEUED), None)
                    if job is None:
                        self._wakeup.wait()
                job.state = JobState.RUNNING
                self._running += 1
            threading.Thread(target=self._run, args=(job,), name=f"fta-job-{job.id}", daemon=True).start()

    def _tick(self) -> None:
        while True:
            time.sleep(max(self.update_interval, 0.05))
            for job in list(self.jobs):
                if job._dirty and job.state is JobState.RUNNING:
                    job._dirty = False
                    self._notify(job)

    def _run(self, job: TransferJob) -> None:
        self._notify(job)
        last_update = 0.0

        def on_file(path: str) -> None:
            if job.current_file:
                job.files_done += 1
            job.current_file = path
            job.bytes_done, job.bytes_total, job.speed = 0, None, 0.0
            job._file_started = time.monotonic()
            self._notify(job)

        def on_progress(done: int, total: int | None) -> None:
            nonlocal last_update
            if job._cancel.is_set():
                raise _Cancelled
            job.bytes_done, job.bytes_total = done, total
            now = time.monotonic()
            elapsed = now - job._file_started
            job.speed = done / elapsed if elapsed > 0 else 0.0
            if now - last_update >= self.update_interval:
                last_update = now
                job._dirty = False
                self._notify(job)
            else:
                job._dirty = True  # the ticker delivers it if no further progress comes

        try:
            copy(
                job.source_url,
                job.destination_url,
                progress=on_progress,
                recursive=job.recursive,
                on_file=on_file,
                source_options=job.source_options,
                destination_options=job.destination_options,
            )
        except _Cancelled:
            job.state = JobState.CANCELLED
        except Exception as exc:  # any failure belongs to this job, never to the worker
            job.state = JobState.CANCELLED if job._cancel.is_set() else JobState.FAILED
            job.error = str(exc) or type(exc).__name__
        else:
            job.files_done += 1
            job.state = JobState.DONE
        with self._lock:
            self._running -= 1
            self._wakeup.notify()
        self._notify(job)

    def _notify(self, job: TransferJob) -> None:
        for listener in list(self._listeners):
            try:
                listener(job)
            except Exception:  # a broken view must not kill transfers, but must not go unnoticed
                logger.exception("transfer listener %r failed", listener)
