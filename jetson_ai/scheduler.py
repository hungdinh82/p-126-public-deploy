from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager


class PriorityGpuScheduler:
    """One-heavy-job scheduler: STT > LLM > TTS, with bounded admission."""

    PRIORITY = {"stt": 0, "llm": 1, "tts": 2}

    def __init__(self) -> None:
        self._owner: asyncio.Task | None = None
        self._capability: str | None = None
        self._waiters: list[tuple[int, int, asyncio.Future, str, asyncio.Task]] = []
        self._counter = 0
        self._lock = asyncio.Lock()

    def locked(self) -> bool:
        return self._owner is not None

    @property
    def active_capability(self) -> str | None:
        return self._capability

    async def acquire(self, capability: str = "llm", timeout: float = 2.0) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("GPU scheduler requires an asyncio task")
        async with self._lock:
            if self._owner is None:
                self._owner, self._capability = task, capability
                return
            if capability == "stt" and self._capability == "tts" and self._owner is not None:
                self._owner.cancel()
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._counter += 1
            item = (self.PRIORITY[capability], self._counter, future, capability, task)
            heapq.heappush(self._waiters, item)
        try:
            await asyncio.wait_for(future, timeout)
        except BaseException:
            async with self._lock:
                if not future.done():
                    future.cancel()
            raise

    def release(self) -> None:
        self._owner = None
        self._capability = None
        while self._waiters:
            _, _, future, capability, task = heapq.heappop(self._waiters)
            if future.cancelled():
                continue
            self._owner, self._capability = task, capability
            future.set_result(True)
            break

    @asynccontextmanager
    async def slot(self, capability: str, timeout: float = 2.0):
        await self.acquire(capability, timeout)
        try:
            yield
        finally:
            self.release()

    async def __aenter__(self):
        await self.acquire("llm")
        return self

    async def __aexit__(self, *_):
        self.release()
