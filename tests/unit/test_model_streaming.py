"""Streaming protocol, cancellation and GPU reservation regression tests."""

import asyncio
import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from jetson_ai.app import ServiceConfig, create_app
from jetson_ai.speech import SpeechModels
from jetson_ai.stream_routes import ModelStreams
from jetson_ai.streaming import sse, worker


def payload(**extra):
    return {"messages": [{"role": "user", "content": "xin chào"}], "stream": True, **extra}


def backend(request):
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    assert json.loads(request.content)["stream"] is True
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b"".join(
            [
                sse({"choices": [{"delta": {"content": "Xin "}}]}),
                sse({"choices": [{"delta": {"content": "chào"}}]}),
                sse("[DONE]"),
            ]
        ),
    )


@pytest.mark.asyncio
async def test_chat_stream_and_canonical_envelope_keep_existing_contract():
    app = create_app(ServiceConfig(token="", auth_required=False), transport=httpx.MockTransport(backend))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json=payload())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.count("data:") == 3 and "[DONE]" in response.text
        response = await client.post(
            "/v1/llm/generate",
            json={
                "contract_version": "1.0",
                "capability": "llm",
                "request_id": "req",
                "call_id": "call",
                "attempt": 2,
                "stream": True,
                "input": payload(stream=False),
            },
        )
        assert "event: delta" in response.text and "event: done" in response.text
        result = json.loads(response.text.split("event: done\ndata: ")[1].strip())
        assert result["result"]["text"] == "Xin chào" and result["call_id"] == "call"
        assert result["first_chunk_ms"] >= 0
        assert (await client.post("/v1/chat/completions", json=payload(stream="true"))).status_code == 400


@pytest.mark.asyncio
async def test_incomplete_llm_stream_reports_error_without_success():
    def broken(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse({"choices": [{"delta": {"content": "partial"}}]}),
        )

    app = create_app(ServiceConfig(token="", auth_required=False), transport=httpx.MockTransport(broken))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/v1/llm/generate",
            json={
                "contract_version": "1.0",
                "capability": "llm",
                "request_id": "r",
                "call_id": "c",
                "attempt": 1,
                "input": payload(),
            },
        )
        assert "event: error" in response.text and "event: done" not in response.text


@pytest.mark.asyncio
async def test_streamed_tool_arguments_are_assembled_only_in_done():
    def reply(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        frames = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "tool-1",
                                    "function": {"name": "interpret_turn", "arguments": '{"act":'},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"question"}'}}]}}]},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join([*(sse(frame) for frame in frames), sse("[DONE]")]),
        )

    app = create_app(ServiceConfig(token="", auth_required=False), transport=httpx.MockTransport(reply))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/v1/llm/generate",
            json={
                "contract_version": "1.0",
                "capability": "llm",
                "request_id": "r",
                "call_id": "c",
                "attempt": 1,
                "input": payload(),
            },
        )
        result = json.loads(response.text.split("event: done\ndata: ")[1].strip())
        tool = result["result"]["message"]["tool_calls"][0]
        assert tool["id"] == "tool-1" and tool["function"]["name"] == "interpret_turn"
        assert json.loads(tool["function"]["arguments"]) == {"act": "question"}


@pytest.mark.asyncio
async def test_total_stream_deadline_even_when_backend_keeps_sending_heartbeats():
    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": heartbeat\n\n"
                await asyncio.sleep(0.01)

    def reply(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Endless())

    app = create_app(
        ServiceConfig(token="", auth_required=False, timeout_seconds=0.05), transport=httpx.MockTransport(reply)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await asyncio.wait_for(client.post("/v1/chat/completions", json=payload()), 1)
        assert "SERVICE_TIMEOUT" in response.text


@pytest.mark.asyncio
async def test_first_llm_chunk_arrives_before_backend_finishes_and_abort_releases_slot():
    finish = asyncio.Event()
    closed = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"choices": [{"delta": {"content": "first"}}]})
            await finish.wait()
            yield sse("[DONE]")

        async def aclose(self):
            closed.append(True)

    def response(_):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowStream())

    async def ready():
        return "ready"

    slots = asyncio.Semaphore(1)
    streams = ModelStreams(
        ServiceConfig(token=""), None, slots, ready, None, lambda *_: None, httpx.MockTransport(response)
    )
    result = await streams.chat(payload(), Request({"type": "http", "headers": []}))
    assert b"first" in await asyncio.wait_for(anext(result.body_iterator), 1)
    assert not finish.is_set() and slots.locked()
    await result.body_iterator.aclose()
    assert closed and not slots.locked()


@pytest.mark.asyncio
async def test_cancellation_waits_for_native_inference_before_cleanup():
    started, finish = threading.Event(), threading.Event()

    def native_step():
        started.set()
        finish.wait(5)

    task = asyncio.create_task(worker(native_step))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_pcm_stream_is_incremental_and_releases_gpu_on_disconnect():
    class Speech:
        info = {"tts": {"status": "ready", "sample_rate": 48000}}

        def synthesize_stream(self, text):
            try:
                yield b"\x01\x00"
                raise AssertionError("Client disconnected before the second decoder step")
            finally:
                self.closed = True

    speech = Speech()
    slots = asyncio.Semaphore(1)
    streams = ModelStreams(ServiceConfig(token=""), speech, slots, None, None, None)
    response = await streams.tts("hello", None)
    assert response.headers["x-audio-format"] == "pcm_s16le"
    assert await anext(response.body_iterator) == b"\x01\x00"
    assert slots.locked()
    await response.body_iterator.aclose()
    assert speech.closed and not slots.locked()
    assert speech.info["tts"]["status"] == "ready"


def speech_app(auth=False):
    app = create_app(ServiceConfig(token="s" * 32, auth_required=auth))
    speech = app.state.speech
    speech.info["stt"]["status"] = "ready"
    speech.transcribe = lambda audio: "Xin chào"
    return app


def test_stt_socket_auth_partial_final_and_revised_transcript():
    app = speech_app(auth=True)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/stt/stream") as socket:
            socket.send_json({"type": "start", "sample_rate": 16000, "format": "pcm_s16le"})
            assert socket.receive_json()["error"]["code"] == "UNAUTHORIZED"
        with client.websocket_connect("/v1/stt/stream") as socket:
            socket.send_json({"type": "start", "sample_rate": 16000, "format": "pcm_s16le", "token": "s" * 32})
            assert socket.receive_json()["type"] == "ready"
            socket.send_bytes(b"\0\0" * 16000)
            assert socket.receive_json()["type"] == "partial"
            assert socket.receive_json()["type"] == "ack"
            app.state.speech.transcribe = lambda audio: "Xin chào bạn"
            socket.send_text("stop")
            assert socket.receive_json()["text"] == "Xin chào bạn"


def test_stt_socket_rejects_wrong_pcm_size_and_remains_available():
    with TestClient(speech_app()) as client:
        with client.websocket_connect("/v1/stt/stream") as socket:
            socket.send_json({"type": "start", "sample_rate": 16000, "format": "pcm_s16le"})
            socket.receive_json()
            socket.send_bytes(b"odd")
            assert socket.receive_json()["error"]["code"] == "INVALID_REQUEST"
        with client.websocket_connect("/v1/stt/stream") as socket:
            socket.send_json({"type": "start", "sample_rate": 16000, "format": "pcm_s16le"})
            assert socket.receive_json()["type"] == "ready"
            socket.send_bytes(b"\0\0" * 100)
            assert socket.receive_json()["type"] == "ack"
            socket.send_text("stop")
            assert socket.receive_json()["type"] == "done"


def test_whisper_does_not_drop_audio_after_first_30_seconds(monkeypatch):
    import io
    import sys
    import wave

    sizes = []

    class Processor:
        def __call__(self, samples, **kwargs):
            sizes.append(len(samples))
            return SimpleNamespace(input_features=SimpleNamespace(to=lambda **_: None))

        def batch_decode(self, *args, **kwargs):
            return ["first" if len(sizes) == 1 else "last"]

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    speech = SpeechModels()
    speech.stt_engine = "transformers"
    speech.models["stt"] = SimpleNamespace(device="cpu", dtype=None, generate=lambda *_, **__: None), Processor()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 16000 * 31)
    assert speech.transcribe(audio.getvalue()) == "first last"
    assert sizes == [16000 * 30, 16000]
