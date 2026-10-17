"""A replaced voice revision must stop the gateway's upstream generation."""

import asyncio
import json

import httpx
import pytest

from jetson_ai.app import ServiceConfig, create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_http_disconnect_cancels_qwen_and_releases_inference_lane(stream):
    entered, disconnected, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def backend(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    app = create_app(ServiceConfig(token="", auth_required=False), transport=httpx.MockTransport(backend))
    body = json.dumps({"messages": [{"role": "user", "content": "first revision"}], "stream": stream}).encode()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    running = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        disconnected.set()
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.wait_for(running, 1)
        assert not app.state.scheduler._active
        assert not app.state.scheduler._pending
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
