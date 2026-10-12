"""Keep the navigation writer until an already dispatched vehicle effect settles."""

import asyncio


async def vehicle_effect(function, *args, **kwargs):
    work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Shutdown may cancel an already cancelled request a second time.
        # Neither cancellation may release its writer ahead of the effect.
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if work.done() and not work.cancelled():
            work.exception()
        raise
