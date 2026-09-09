"""Thread-safe sliding-window budget for outgoing bot interactions."""

from __future__ import annotations

import math
import threading
import time
from collections import deque


class BotRateLimitError(RuntimeError):
    pass


class BotRateLimiter:
    def __init__(self, count: int, seconds: float, *, clock=time.monotonic):
        self.count = count
        self.seconds = seconds
        self.clock = clock
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            while self._sent and now - self._sent[0] >= self.seconds:
                self._sent.popleft()
            if len(self._sent) >= self.count:
                wait = max(1, math.ceil(self.seconds - (now - self._sent[0])))
                raise BotRateLimitError(
                    f"Bot 请求达到限制（每 {self.seconds:g} 秒 {self.count} 次），请在 {wait} 秒后重试"
                )
            self._sent.append(now)
