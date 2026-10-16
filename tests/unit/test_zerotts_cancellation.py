"""A browser disconnect must not close a native generator during inference."""

import asyncio
import threading
from types import SimpleNamespace

import anyio
import numpy as np
import pytest

from src.vivi.config import Settings
from src.vivi.speech.tts import ZeroTTSAdapter


@pytest.mark.asyncio
async def test_level_cancellation_waits_for_native_step_before_closing_and_unlocking():
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def chunks(text, voice):
        try:
            entered.set()
            assert release.wait(3), "Test did not release the native step"
            yield np.array([0.1], dtype=np.float32)
        finally:
            closed.set()

    adapter = ZeroTTSAdapter(Settings())
    adapter._model = SimpleNamespace(synthesize_stream=chunks)
    adapter._voice = object()

    async def consume():
        async for _ in adapter.stream("Xin chào"):
            pass

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            assert await asyncio.to_thread(entered.wait, 2)
            group.cancel_scope.cancel()
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0.02)
                assert adapter._lock.locked()
                assert not closed.is_set()
                release.set()
    finally:
        release.set()
    assert closed.is_set()
    assert not adapter._lock.locked()
