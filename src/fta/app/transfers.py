"""Background transfer queue: priorities, pause, automatic retries, journal.

Jobs run on worker threads and listeners get throttled updates. Pausing stops
the transfer and resuming starts it again with ``resume=True``, so the bytes
already at the destination are not sent twice; automatic retries resume too.
"""

from __future__ import annotations

import enum
import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fta.core.errors import IntegrityError
from fta.core.journal import Journal, JournalEntry
from fta.core.manager import FileResult, copy
from fta.core.retry import is_retryable, retry_delay

logger = logging.getLogger(__name__)

class JobState(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class _Stop(Exception):
    """Raised from the progress callback to stop a running transfer."""


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
    priority: int = 0
    """Higher runs first; equal priorities run in the order they were added."""
    max_retries: int = 0
    verify: bool = False
    action: Any = None
    """An ``Action`` run instead of ``copy()``, e.g. a send to a nearby device."""

    id: int = field(default_factory=lambda: next(_ids))
    state: JobState = JobState.QUEUED
    current_file: str = ""
    files_done: int = 0
    bytes_done: int = 0
    bytes_total: int | None = None
    speed: float = 0.0
    """Bytes per second for the current file."""
    error: str = ""
    attempts: int = 0
    """Runs started so far, including the first one."""
    retry_at: float = 0.0
    """time.monotonic() before which a queued retry must not start."""
    results: list[FileResult] = field(default_factory=list)

    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _stop_state: JobState = field(default=JobState.CANCELLED, repr=False)
    _resume: bool = field(default=False, repr=False)
    """Continue partial files on the next run (after a pause or a failed attempt)."""
    _file_started: float = field(default=0.0, repr=False)
    _resumed_bytes: int = field(default=0, repr=False)
    _dirty: bool = field(default=False, repr=False)
    """Progress changed since listeners last heard about it."""

    @property
    def fraction(self) -> float | None:
        if self.state is JobState.DONE:
            return 1.0
        if self.bytes_total:
            return min(self.bytes_done / self.bytes_total, 1.0)
        return None

    @property
    def waiting_for_retry(self) -> float:
        """Seconds until a scheduled retry starts, 0 when none is pending."""
        if self.state is not JobState.QUEUED:
            return 0.0
        return max(0.0, self.retry_at - time.monotonic())

    def cancel(self) -> None:
        self._stop_state = JobState.CANCELLED
        self._stop.set()


Listener = Callable[[TransferJob], None]
Action = Callable[[Callable[[int, int | None], None], Callable[[str], None], bool], list[FileResult]]
"""A job that is not a ``copy()``: ``action(progress, on_file, resume)`` returns the files sent."""


class TransferQueue:
    """Runs jobs with at most ``parallel`` at a time.

    Listeners are called from worker threads on every state change and at most
    ``update_interval`` seconds apart while bytes flow. Progress that arrives in
    a burst and then stalls (a fast write into socket buffers, then a long wait
    for the server) is still delivered by the ticker, so views never stay stale.
    """

    def __init__(
        self,
        parallel: int = 2,
        update_interval: float = 0.15,
        journal: Journal | None = None,
        delay: Callable[[int], float] = retry_delay,
    ) -> None:
        self.jobs: list[TransferJob] = []
        self.parallel = parallel
        self.update_interval = update_interval
        self.journal = journal
        self.delay = delay
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
        """Queue a fresh copy of a failed or cancelled job; partial files are continued."""
        fresh = TransferJob(
            name=job.name,
            source=job.source,
            destination=job.destination,
            source_url=job.source_url,
            destination_url=job.destination_url,
            recursive=job.recursive,
            source_options=job.source_options,
            destination_options=job.destination_options,
            priority=job.priority,
            max_retries=job.max_retries,
            verify=job.verify,
            action=job.action,
        )
        fresh._resume = True
        return self.add(fresh)

    def cancel(self, job: TransferJob) -> None:
        job.cancel()
        with self._lock:
            if job.state in (JobState.QUEUED, JobState.PAUSED):
                job.state = JobState.CANCELLED
        self._notify(job)

    def pause(self, job: TransferJob) -> None:
        with self._lock:
            if job.state is JobState.QUEUED:
                job.state = JobState.PAUSED
            elif job.state is JobState.RUNNING:
                job._stop_state = JobState.PAUSED
                job._stop.set()
        self._notify(job)

    def resume(self, job: TransferJob) -> None:
        with self._lock:
            if job.state is JobState.PAUSED:
                job.state = JobState.QUEUED
                job.retry_at = 0.0
                job._resume = True
                self._wakeup.notify()
        self._notify(job)

    def set_priority(self, job: TransferJob, priority: int) -> None:
        with self._lock:
            job.priority = priority
            self._wakeup.notify()
        self._notify(job)

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [job for job in self.jobs if not job.state.finished]

    @property
    def active_count(self) -> int:
        return sum(1 for job in self.jobs if job.state in (JobState.QUEUED, JobState.RUNNING))

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until no job is queued or running; for tests and shutdown."""
        deadline = time.monotonic() + timeout
        while self.active_count:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def _next_job(self) -> tuple[TransferJob | None, float | None]:
        """The job to start now, or how long to wait for the earliest scheduled retry."""
        now = time.monotonic()
        ready = [j for j in self.jobs if j.state is JobState.QUEUED and j.retry_at <= now]
        if ready:
            return max(ready, key=lambda j: (j.priority, -j.id)), None
        waits = [j.retry_at - now for j in self.jobs if j.state is JobState.QUEUED]
        return None, (min(waits) if waits else None)

    def _dispatch(self) -> None:
        while True:
            with self._lock:
                while True:
                    job, wait = (None, None)
                    if self._running < self.parallel:
                        job, wait = self._next_job()
                    if job is not None:
                        break
                    self._wakeup.wait(timeout=wait)
                job.state = JobState.RUNNING
                job.attempts += 1
                job._stop.clear()
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
        job.error = ""

        def on_file(path: str) -> None:
            if job.current_file:
                job.files_done += 1
            job.current_file = path
            job.bytes_done, job.bytes_total, job.speed = 0, None, 0.0
            job._file_started = time.monotonic()
            job._resumed_bytes = -1  # set by the first progress report of this file
            self._notify(job)

        def on_progress(done: int, total: int | None) -> None:
            nonlocal last_update
            if job._stop.is_set():
                raise _Stop
            if job._resumed_bytes < 0:
                job._resumed_bytes = done  # a resumed file starts above zero; not this run's speed
            job.bytes_done, job.bytes_total = done, total
            now = time.monotonic()
            elapsed = now - job._file_started
            job.speed = (done - job._resumed_bytes) / elapsed if elapsed > 0 else 0.0
            if now - last_update >= self.update_interval:
                last_update = now
                job._dirty = False
                self._notify(job)
            else:
                job._dirty = True  # the ticker delivers it if no further progress comes

        job.files_done, job.current_file = 0, ""
        try:
            if job.action is not None:
                job.results = job.action(on_progress, on_file, job._resume)
            else:
                job.results = copy(
                    job.source_url,
                    job.destination_url,
                    progress=on_progress,
                    recursive=job.recursive,
                    resume=job._resume,
                    checksum=self.journal is not None,
                    verify=job.verify,
                    on_file=on_file,
                    source_options=job.source_options,
                    destination_options=job.destination_options,
                )
        except _Stop:
            job.state = job._stop_state
            job._resume = True
        except Exception as exc:  # any failure belongs to this job, never to the worker
            job.error = str(exc) or type(exc).__name__
            if job._stop.is_set():
                job.state = job._stop_state
                job._resume = True
            elif job.attempts <= job.max_retries and is_retryable(exc):
                # A corrupted copy must be sent again from scratch, anything else continues.
                job._resume = not isinstance(exc, IntegrityError)
                job.retry_at = time.monotonic() + self.delay(job.attempts)
                job.state = JobState.QUEUED
                logger.warning("job %s attempt %s failed, retrying: %s", job.id, job.attempts, job.error)
            else:
                job.state = JobState.FAILED
                logger.error("job %s failed: %s", job.id, job.error)
        else:
            job.files_done = len(job.results)
            job.state = JobState.DONE
            logger.info("job %s done: %s -> %s", job.id, job.source_url, job.destination_url)
        if job.state.finished:
            self._write_journal(job)
        with self._lock:
            self._running -= 1
            self._wakeup.notify()
        self._notify(job)

    def _write_journal(self, job: TransferJob) -> None:
        if self.journal is None:
            return
        try:
            if job.state is JobState.DONE:
                for result in job.results:
                    self.journal.append(
                        JournalEntry(
                            source=_label(job.source_url, result.source),
                            destination=_label(job.destination_url, result.destination),
                            size=result.size,
                            status="done",
                            seconds=round(result.seconds, 3),
                            sha256=result.sha256,
                            verified=result.verified,
                            resumed_from=result.resumed_from,
                        )
                    )
            else:
                self.journal.append(
                    JournalEntry(
                        source=job.source_url,
                        destination=job.destination_url,
                        size=job.bytes_done,
                        status=job.state.value,
                        error=job.error,
                    )
                )
        except OSError:
            logger.exception("cannot write the transfer journal")

    def _notify(self, job: TransferJob) -> None:
        for listener in list(self._listeners):
            try:
                listener(job)
            except Exception:  # a broken view must not kill transfers, but must not go unnoticed
                logger.exception("transfer listener %r failed", listener)


def _label(url: str, path: str) -> str:
    """The URL's scheme and host with the file's own path, minus any credentials."""
    if "://" not in url:
        return path
    scheme, rest = url.split("://", 1)
    host = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    return f"{scheme}://{host}{path}"
