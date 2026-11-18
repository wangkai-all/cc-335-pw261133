"""Politeness throttle shared by crawler and parser workers.

A single small module is used instead of a third-party dependency so the whole
application runs on the standard library.  The parser worker also uses this for
its optional rate limit.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rate_per_second: float, burst: float = 1.0):
        self.rate = max(0.0001, float(rate_per_second))
        self.capacity = max(1.0, float(burst))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                time.sleep((tokens - self._tokens) / self.rate)
