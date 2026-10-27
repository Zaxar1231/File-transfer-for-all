"""Timer bomb: received files that delete themselves after the time the sender chose.

The receiving user is told before accepting. Deadlines are kept in a small JSON
file, so files still go when the app was closed at the moment: they are deleted
at the next start.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_SECONDS = 60
MAX_SECONDS = 7 * 24 * 3600


class Burner:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self.pending: dict[str, float] = self._load()
        self.sweep()

    def _load(self) -> dict[str, float]:
        try:
            return {str(k): float(v) for k, v in json.loads(self.state_file.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.pending), encoding="utf-8")

    def schedule(self, paths: list[Path], seconds: float) -> float:
        """Delete ``paths`` in ``seconds``; returns the deadline."""
        deadline = time.time() + seconds
        with self._lock:
            for path in paths:
                self.pending[str(path)] = deadline
            self._save()
        timer = threading.Timer(seconds, self.sweep)
        timer.daemon = True
        timer.start()
        self._timers.append(timer)
        return deadline

    def sweep(self, now: float | None = None) -> list[str]:
        """Delete everything whose time has come; returns what was deleted."""
        now = time.time() if now is None else now
        gone = []
        with self._lock:
            for name, deadline in list(self.pending.items()):
                if deadline > now:
                    continue
                path = Path(name)
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                except OSError as exc:
                    logger.warning("timer bomb: cannot delete %s: %s", path, exc)
                    continue
                del self.pending[name]
                gone.append(name)
            if gone:
                self._save()
        for name in gone:
            logger.info("timer bomb: deleted %s", name)
        return gone

    def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
