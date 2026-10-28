"""Links that hand one file or folder to anyone who opens them: one-time and/or expiring.

The token is 128 random bits in the URL; the link stops working after its first
download (if one-time), when it expires, or when revoked. Folders are sent as a
zip built on the fly.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Share:
    path: Path
    expires_at: float
    one_time: bool = True
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    downloads: int = 0
    revoked: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        return self.path.name + (".zip" if self.path.is_dir() else "")

    @property
    def active(self) -> bool:
        return not self.revoked and time.time() < self.expires_at and not (self.one_time and self.downloads)

    def status(self) -> str:
        if self.revoked:
            return "отозвана"
        if self.one_time and self.downloads:
            return "скачана"
        if time.time() >= self.expires_at:
            return "истекла"
        minutes = int((self.expires_at - time.time()) // 60)
        left = f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{max(minutes, 1)} мин"
        return f"действует ещё {left}" + (" · одноразовая" if self.one_time else f" · скачиваний: {self.downloads}")


class ShareRegistry:
    def __init__(self) -> None:
        self.shares: dict[str, Share] = {}
        self._lock = threading.Lock()

    def add(self, path: Path, ttl_seconds: float, one_time: bool = True) -> Share:
        if not path.exists():
            raise FileNotFoundError(str(path))
        share = Share(path.resolve(), time.time() + ttl_seconds, one_time)
        with self._lock:
            self.shares[share.token] = share
        return share

    def claim(self, token: str) -> Share | None:
        """The share for a download that is starting now, or None if the link is no longer valid.
        A one-time link is used up here, before any byte is sent, so two clicks cannot both win."""
        with self._lock:
            share = self.shares.get(token)
            if share is None or not share.active or not share.path.exists():
                return None
            share.downloads += 1
            return share

    def revoke(self, token: str) -> None:
        with self._lock:
            if token in self.shares:
                self.shares[token].revoked = True

    def listing(self) -> list[Share]:
        with self._lock:
            return sorted(self.shares.values(), key=lambda s: -s.created_at)
