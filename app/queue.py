"""A bounded admission queue that keeps a tiny container from being overrun."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator


class QueueFull(Exception):
    """Raised when the queue cannot promise to start this request in time."""

    def __init__(self, queued: int, retry_after: int, reason: str = "full") -> None:
        super().__init__("queue is full")
        self.queued = queued
        self.retry_after = retry_after
        # "full": no waiting room left. "slow": there is room, but the measured
        # service rate says this caller would time out before starting.
        self.reason = reason


class QueueTimeout(Exception):
    """Raised when a request waited for a worker slot for too long."""

    def __init__(self, waited: int) -> None:
        super().__init__("timed out waiting for a worker slot")
        self.waited = waited


class TaskQueue:
    """FIFO admission control: at most `concurrency` run, at most `capacity` wait."""

    def __init__(self, name: str, concurrency: int, capacity: int, wait_timeout: int) -> None:
        self.name = name
        self.concurrency = max(1, concurrency)
        self.capacity = max(0, capacity)
        self.wait_timeout = wait_timeout
        self._admitted = 0
        self._running = 0
        # Built on first use: asyncio primitives must belong to the loop that runs them.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None
        # Exponentially weighted mean service time, seeded on the first completion.
        self._mean_seconds: float | None = None
        self.completed = 0
        self.rejected_full = 0
        self.rejected_slow = 0
        self.timed_out = 0

    def _bind(self) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._lock = asyncio.Lock()
            self._admitted = 0
            self._running = 0
        assert self._semaphore is not None and self._lock is not None
        return self._semaphore, self._lock

    @property
    def waiting(self) -> int:
        return self._admitted - self._running

    def stats(self) -> dict[str, object]:
        return {
            "concurrency": self.concurrency,
            "capacity": self.capacity,
            "running": self._running,
            "waiting": self.waiting,
            "meanServiceMs": round(self._mean_seconds * 1000) if self._mean_seconds else None,
            "estimatedWaitSeconds": round(self.estimated_wait(), 1),
            "completed": self.completed,
            "rejectedFull": self.rejected_full,
            "rejectedSlow": self.rejected_slow,
            "timedOut": self.timed_out,
        }

    def observe(self, seconds: float) -> None:
        self.completed += 1
        if self._mean_seconds is None:
            self._mean_seconds = seconds
        else:
            self._mean_seconds += 0.25 * (seconds - self._mean_seconds)

    def estimated_wait(self, extra: int = 0) -> float:
        """How long a caller joining now would wait, from measured service time."""
        if self._mean_seconds is None:
            return 0.0
        ahead = self.waiting + extra
        return (ahead / self.concurrency) * self._mean_seconds

    def _retry_after(self) -> int:
        """Seconds until a slot should free up, used for the Retry-After header."""
        estimate = self.estimated_wait()
        if estimate <= 0:
            per_slot = max(1, self.wait_timeout // max(1, self.concurrency))
            return max(1, min(self.wait_timeout, per_slot * (1 + self.waiting // self.concurrency)))
        return max(1, round(estimate))

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[int]:
        """Admit the caller, yielding its queue position (0 means it ran immediately)."""
        semaphore, lock = self._bind()
        async with lock:
            if self._admitted >= self.concurrency + self.capacity:
                self.rejected_full += 1
                raise QueueFull(queued=self.waiting, retry_after=self._retry_after())
            # Say no now rather than after a long wait that ends in the same answer.
            estimate = self.estimated_wait(extra=1)
            if estimate > self.wait_timeout:
                self.rejected_slow += 1
                raise QueueFull(
                    queued=self.waiting, retry_after=max(1, round(estimate)), reason="slow"
                )
            position = max(0, self._admitted - self.concurrency + 1)
            self._admitted += 1

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=self.wait_timeout)
        except asyncio.TimeoutError:
            async with lock:
                self._admitted -= 1
            self.timed_out += 1
            raise QueueTimeout(waited=self.wait_timeout) from None
        except BaseException:
            async with lock:
                self._admitted -= 1
            raise

        async with lock:
            self._running += 1
        started = time.monotonic()
        try:
            yield position
        finally:
            self.observe(time.monotonic() - started)
            async with lock:
                self._running -= 1
                self._admitted -= 1
            semaphore.release()
