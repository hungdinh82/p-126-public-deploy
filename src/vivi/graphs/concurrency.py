"""Release the checkpoint writer during external reads; fence stale results."""

import asyncio
import heapq
from contextvars import ContextVar
from itertools import count

event_priority = ContextVar("navigation_event_priority", default=2)


class PriorityLock:
    """A single checkpoint writer; parking precedes queued telemetry."""

    def __init__(self):
        self._held = False
        self._waiting = []
        self._order = count()

    async def acquire(self):
        if not self._held:
            self._held = True
            return True
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiting, (event_priority.get(), next(self._order), future))
        try:
            await future
            return True
        except BaseException:
            if future.done() and not future.cancelled():
                self.release()
            else:
                future.cancel()
            raise

    def release(self):
        while self._waiting:
            _, _, future = heapq.heappop(self._waiting)
            if not future.done():
                future.set_result(True)
                return
        self._held = False

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()


class SupersededTurnError(Exception):
    pass


writer_context = ContextVar("checkpoint_writer", default=None)


async def external_read(awaitable):
    context = writer_context.get()
    if context is None:
        return await awaitable
    graph, config, lock = context
    generation = graph.generation(config)
    lock.release()
    error = None
    try:
        result = await awaitable
    except BaseException as exc:
        error = exc
    finally:
        await lock.acquire()
    if graph.generation(config) != generation:
        raise SupersededTurnError("Session changed while external data was being fetched")
    if error:
        raise error
    return result
