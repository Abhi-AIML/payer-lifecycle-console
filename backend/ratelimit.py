"""A sliding-window limit on agent runs per account, so a public demo cannot exhaust the Gemini free-tier quota.

In memory, like the rest of the demo state. That is enough because the service runs as a single instance.
"""
import threading
import time
from collections import deque


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> int:
        """Records one use. Returns 0 if allowed, otherwise the seconds to wait. A limit of 0 or less disables it."""
        if self.limit <= 0:
            return 0
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return int(self.window - (now - hits[0])) + 1
            hits.append(now)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
