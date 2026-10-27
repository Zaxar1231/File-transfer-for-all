"""A speed limit shared by every transfer in the process (a token bucket).

Transfers report bytes as they move; ``consume`` sleeps just long enough to keep
the total under the limit. Protocol libraries call progress from their own
read/write loop, so slowing the callback slows the transfer itself.
"""

from __future__ import annotations

import threading
import time


class Limiter:
    def __init__(self, bytes_per_second: float = 0) -> None:
        self._lock = threading.Lock()
        self.rate = 0.0
        self._tokens = 0.0
        self._stamp = time.monotonic()
        self.set_rate(bytes_per_second)

    def set_rate(self, bytes_per_second: float) -> None:
        """0 removes the limit."""
        with self._lock:
            self.rate = max(0.0, float(bytes_per_second))
            self._tokens = self._burst
            self._stamp = time.monotonic()

    @property
    def _burst(self) -> float:
        return max(self.rate / 4, 64 * 1024)  # a quarter second of data: smooth, not stuttering

    def consume(self, count: int) -> None:
        if count <= 0 or not self.rate:
            return
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            self._tokens -= count
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait > 0:
            time.sleep(wait)


LIMITER = Limiter()
"""The process-wide limit; the app sets it from Settings, ``fta cp --limit`` from the command line."""


def parse_rate(text: str) -> int:
    """``500K``, ``2M``, ``1.5M`` or plain bytes per second → bytes per second; ``0`` means no limit."""
    text = text.strip().upper().removesuffix("/S").removesuffix("B")
    factor = {"K": 1024, "M": 1024**2, "G": 1024**3}.get(text[-1:], 1)
    number = text[:-1] if factor > 1 else text
    value = float(number)
    if value < 0:
        raise ValueError("a speed limit cannot be negative")
    return int(value * factor)


class Metered:
    """Wraps a ProgressCallback so every byte reported also passes the limiter."""

    def __init__(self, callback, limiter: Limiter = LIMITER) -> None:
        self.callback = callback
        self.limiter = limiter
        self._last = 0

    def __call__(self, done: int, total: int | None) -> None:
        if done < self._last:  # a new file (or a restart) began counting from zero
            self._last = 0
        self.limiter.consume(done - self._last)
        self._last = done
        if self.callback:
            self.callback(done, total)
