from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Condition


class ZipQueueTimeout(TimeoutError):
    """Raised when a ZIP build cannot obtain a queue slot in time."""


class ZipBuildQueue:
    """A FIFO, in-process concurrency limit for CPU-intensive ZIP creation."""

    def __init__(self, max_concurrent: int) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._active = 0
        self._waiting: deque[object] = deque()
        self._condition = Condition()

    @contextmanager
    def slot(self, timeout_seconds: float) -> Iterator[None]:
        ticket = object()
        deadline = time.monotonic() + max(0, timeout_seconds)
        with self._condition:
            self._waiting.append(ticket)
            while self._waiting[0] is not ticket or self._active >= self._max_concurrent:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiting.remove(ticket)
                    self._condition.notify_all()
                    raise ZipQueueTimeout("ZIP build queue wait timed out")
                self._condition.wait(remaining)
            self._waiting.popleft()
            self._active += 1

        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
