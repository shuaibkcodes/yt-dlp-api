"""A pool of warm yt-dlp worker processes, multiplexed over pipes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from itertools import count

_IDS = count(1)


class WorkerError(Exception):
    """yt-dlp failed for this request; the message came back from the worker."""

    def __init__(self, message: str, reason: str = "extraction_failed") -> None:
        super().__init__(message)
        self.reason = reason


class PoolUnavailable(Exception):
    """No worker could take the request."""


class _Worker:
    def __init__(self, index: int, threads: int, env: dict[str, str], startup_timeout: float) -> None:
        self.index = index
        self.threads = threads
        self.env = env
        self.startup_timeout = startup_timeout
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.restarts = 0
        self._reader: asyncio.Task | None = None
        self._starting = asyncio.Lock()
        self._ready = asyncio.Event()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def inflight(self) -> int:
        return len(self.pending)

    async def start(self) -> None:
        async with self._starting:
            if self.alive:
                return
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                env={**os.environ, **self.env, "WORKER_THREADS": str(self.threads)},
            )
            self._reader = asyncio.create_task(self._read_loop(self.process))
            # The worker announces itself once yt-dlp is imported and it can serve.
            await asyncio.wait_for(self._ready.wait(), timeout=self.startup_timeout)

    async def _read_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is None:
                    self._ready.set()
                    continue
                future = self.pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                if message.get("ok"):
                    future.set_result(message.get("result"))
                else:
                    future.set_exception(
                        WorkerError(
                            message.get("error") or "yt-dlp failed",
                            message.get("reason") or "extraction_failed",
                        )
                    )
        finally:
            self._fail_pending(PoolUnavailable("worker exited"))

    def _fail_pending(self, error: BaseException) -> None:
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        self.pending.clear()
        self._ready.clear()

    async def kill(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError, OSError):
                process.kill()
            with suppress(Exception):
                await process.wait()
        self._fail_pending(PoolUnavailable("worker recycled"))
        self.restarts += 1


class WorkerPool:
    """Keeps `size` warm workers and routes each request to the least busy one."""

    def __init__(
        self,
        size: int,
        threads: int,
        startup_timeout: float = 120.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.size = max(1, size)
        self.threads = max(1, threads)
        self.startup_timeout = startup_timeout
        self.env = env or {}
        self._workers: list[_Worker] = []
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._respawns: set[asyncio.Task] = set()

    def _bind(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._lock = asyncio.Lock()
            self._workers = []
        assert self._lock is not None
        return self._lock

    def stats(self) -> dict[str, object]:
        return {
            "size": self.size,
            "threadsPerWorker": self.threads,
            "alive": sum(1 for w in self._workers if w.alive),
            "inflight": sum(w.inflight for w in self._workers),
            "restarts": sum(w.restarts for w in self._workers),
        }

    async def start(self) -> None:
        lock = self._bind()
        async with lock:
            if not self._workers:
                self._workers = [
                    _Worker(i, self.threads, self.env, self.startup_timeout)
                    for i in range(self.size)
                ]
        # Warm every worker up front so the first request does not pay the import.
        await asyncio.gather(*(self._ensure(w) for w in self._workers), return_exceptions=True)

    async def _ensure(self, worker: _Worker) -> bool:
        if worker.alive and worker._ready.is_set():
            return True
        try:
            await worker.start()
        except Exception:
            await worker.kill()
            return False
        return True

    async def stop(self) -> None:
        for worker in self._workers:
            await worker.kill()

    def _pick(self) -> _Worker | None:
        """The least busy ready worker; the admission queues cap total in-flight work."""
        candidates = [w for w in self._workers if w.alive and w._ready.is_set()]
        if not candidates:
            return None
        return min(candidates, key=lambda w: w.inflight)

    async def submit(self, op: str, payload: dict, timeout: float) -> object:
        lock = self._bind()
        if not self._workers:
            await self.start()

        worker = self._pick()
        if worker is None:
            # Every worker is down: try to bring one back before giving up.
            async with lock:
                for candidate in self._workers:
                    if await self._ensure(candidate):
                        break
            worker = self._pick()
        if worker is None:
            raise PoolUnavailable("no yt-dlp worker is available")

        request_id = f"r{next(_IDS)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        worker.pending[request_id] = future
        message = json.dumps({"id": request_id, "op": op, **payload}, separators=(",", ":"))
        try:
            assert worker.process is not None and worker.process.stdin is not None
            worker.process.stdin.write(message.encode() + b"\n")
            await worker.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AssertionError, RuntimeError):
            worker.pending.pop(request_id, None)
            await worker.kill()
            raise PoolUnavailable("worker pipe closed") from None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            worker.pending.pop(request_id, None)
            # A thread inside the worker cannot be killed, so recycle the process;
            # otherwise a wedged extraction would hold one of its slots forever.
            await worker.kill()
            # Held in a set: a bare task reference can be collected mid-flight.
            respawn = asyncio.create_task(self._ensure(worker))
            self._respawns.add(respawn)
            respawn.add_done_callback(self._respawns.discard)
            raise
        except asyncio.CancelledError:
            worker.pending.pop(request_id, None)
            raise
