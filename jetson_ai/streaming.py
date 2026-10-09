"""Streaming primitives shared by native and Python model gateways."""

import asyncio
import json

import anyio


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
