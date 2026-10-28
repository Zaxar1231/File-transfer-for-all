"""Runs saved schedules: at their time of day each one becomes a job in the transfer queue.

A schedule missed while the app was closed runs once when the app starts again
the same day; the check runs every 20 seconds, so times are kept to the minute.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from fta.app.storage import Schedule, Store
from fta.app.transfers import TransferJob, TransferQueue
from fta.core.manager import FileResult

logger = logging.getLogger(__name__)

CHECK_EVERY = 20.0


def job_for(schedule: Schedule, store: Store) -> TransferJob:
    """The queue job that performs one run of ``schedule``."""
    source, source_options = store.resolve(schedule.source)
    destination, destination_options = store.resolve(schedule.destination)
    common: dict[str, Any] = dict(
        name=f"⏰ {schedule.name}",
        source=schedule.source,
        destination=schedule.destination,
        source_url=source,
        destination_url=destination,
        source_options=source_options,
        destination_options=destination_options,
        max_retries=store.settings.retries,
    )
    if schedule.mode == "copy":
        return TransferJob(recursive=True, verify=store.settings.verify, **common)
    return TransferJob(action=sync_action(schedule, store, source, destination, source_options, destination_options), **common)


def sync_action(
    schedule: Schedule, store: Store, source: str, destination: str, source_options: dict, destination_options: dict
) -> Callable[..., list[FileResult]]:
    from fta.core.sync import sync

    def action(progress, on_file, resume) -> list[FileResult]:
        started = time.monotonic()
        report = sync(
            source,
            destination,
            manifest=store.data_dir / "sync-manifest.json",
            delete=schedule.delete,
            progress=progress,
            on_file=on_file,
            source_options=source_options,
            destination_options=destination_options,
        )
        logger.info("schedule %r: %s", schedule.name, report.summary())
        elapsed = time.monotonic() - started
        # resumed_from carries the bytes delta transfer did not send, so the journal shows the saving.
        return [
            FileResult(f"{source.rstrip('/')}/{rel}", f"/{rel}", size, elapsed, sha, False, size - sent)
            for rel, size, sent, sha in report.sent
        ]

    return action


class Scheduler:
    def __init__(self, store: Store, queue: TransferQueue, on_run: Callable[[Schedule], None] | None = None) -> None:
        self.store = store
        self.queue = queue
        self.on_run = on_run or (lambda schedule: None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="fta-scheduler", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_now(self, schedule: Schedule) -> TransferJob:
        job = self.queue.add(job_for(schedule, self.store))
        schedule.last_run = time.time()
        self.store.save()
        self.on_run(schedule)
        return job

    def tick(self, now: float | None = None) -> list[Schedule]:
        """Start every schedule that is due; returns the ones started (for tests and the UI)."""
        now = time.time() if now is None else now
        started = []
        for schedule in list(self.store.schedules):
            if schedule.due(now) is None:
                continue
            try:
                self.queue.add(job_for(schedule, self.store))
            except Exception:  # a deleted connection: report it, keep the others running
                logger.exception("schedule %r cannot start", schedule.name)
            schedule.last_run = now
            started.append(schedule)
        if started:
            self.store.save()
            for schedule in started:
                self.on_run(schedule)
        return started

    def _loop(self) -> None:
        while True:  # first check at start-up: catches a run missed while the app was closed
            try:
                self.tick()
            except Exception:  # never let a bad entry stop the clock
                logger.exception("scheduler tick failed")
            if self._stop.wait(CHECK_EVERY):
                return


def describe(schedule: Schedule) -> str:
    names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    days = "каждый день" if len(schedule.days) == 7 else ", ".join(names[d] for d in sorted(schedule.days))
    what = "синхронизация" + (" с удалением" if schedule.delete else "") if schedule.mode == "sync" else "копирование"
    return f"{what} · {days} в {schedule.time}"

