"""Transfer journal: one JSON line per copied file, exportable to CSV."""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class JournalEntry:
    source: str
    destination: str
    size: int
    status: str
    """"done", "failed" or "cancelled"."""
    seconds: float = 0.0
    sha256: str = ""
    verified: bool = False
    resumed_from: int = 0
    error: str = ""
    time: float = field(default_factory=time.time)
    """When the transfer finished, POSIX timestamp."""


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, entry: JournalEntry) -> None:
        line = json.dumps(asdict(entry), ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

    def entries(self, limit: int | None = None) -> list[JournalEntry]:
        """Newest first. Lines that cannot be read (a crash mid-write) are skipped."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        known = {f.name for f in fields(JournalEntry)}
        result = []
        for line in reversed(lines):
            try:
                data = json.loads(line)
                result.append(JournalEntry(**{k: v for k, v in data.items() if k in known}))
            except (ValueError, TypeError):
                continue
            if limit is not None and len(result) >= limit:
                break
        return result

    def export_csv(self, target: Path) -> Path:
        columns = [f.name for f in fields(JournalEntry)]
        with target.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=columns)
            writer.writeheader()
            for entry in reversed(self.entries()):
                row = asdict(entry)
                row["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.time))
                writer.writerow(row)
        return target
