"""Streaming primitives shared by native and Python model gateways."""

import asyncio
import json

import anyio


class ClientDisconnectedError(Exception):
    pass


async def while_connected(request, operation):
    """Cancel queued/upstream async inference when its HTTP caller disconnects."""

    async def disconnected():
        # Call after the request body has been consumed. A blocking receive is
        # necessary here: is_disconnected's immediate cancellation can miss
        # disconnects delivered through BaseHTTPMiddleware's receive wrapper.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    inference = asyncio.create_task(operation)
    connection = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({inference, connection}, return_when=asyncio.FIRST_COMPLETED)
        if inference in done:
            return await inference
        await connection
        raise ClientDisconnectedError()
    finally:
        for task in (inference, connection):
            if not task.done():
                task.cancel()
        await asyncio.gather(inference, connection, return_exceptions=True)


def sse(data: dict | str, event: str | None = None) -> bytes:
    value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return ((f"event: {event}\n" if event else "") + f"data: {value}\n\n").encode()


def next_or_none(iterator):
    return next(iterator, None)


async def worker(function, *args):
    # A disconnected client must not release the GPU lock during a native step.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await task
        raise


async def close_iterator(iterator):
    with anyio.CancelScope(shield=True):
        close = getattr(iterator, "close", None)
        if close is not None:
            await worker(close)
