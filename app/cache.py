"""A small TTL cache with request coalescing, so repeat lookups cost nothing."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "coalesced": self.coalesced,
        }

    def get(self, key: str) -> object | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: object, ttl: float | None = None) -> None:
        lifetime = self.ttl if ttl is None else ttl
        if lifetime <= 0 or self.max_entries <= 0:
            return
        self._entries[key] = (time.monotonic() + lifetime, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    async def get_or_create(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        ttl_for: Callable[[T], float | None] | None = None,
    ) -> T:
        """Return the cached value, join an identical in-flight call, or run `factory`.

        `ttl_for` can shorten the lifetime of a particular result, so a cached
        failure expires long before a cached success.
        """
        cached = self.get(key)
        if cached is not None:
            self.hits += 1
            return cached  # type: ignore[return-value]

        existing = self._inflight.get(key)
        if existing is not None:
            self.coalesced += 1
            return await asyncio.shield(existing)

        self.misses += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            value = await factory()
        except BaseException as error:
            self._inflight.pop(key, None)
            if not future.done():
                future.set_exception(error)
            # Nobody may be awaiting this future; keep the loop from warning about it.
            future.exception()
            raise
        self._inflight.pop(key, None)
        if not future.done():
            future.set_result(value)
        self.set(key, value, ttl=ttl_for(value) if ttl_for else None)
        return value
