from __future__ import annotations

import json

import pytest
import websockets

from src.vivi.config import Settings
from src.vivi.speech.soniox import SonioxAdapter
from src.vivi.speech.stt import create_stt


def _settings(url: str = "ws://127.0.0.1:1", api_key: str = "test-key") -> Settings:
    return Settings(
        stt_provider="soniox",
        soniox_api_key=api_key,
        soniox_ws_url=url,
        soniox_timeout_seconds=5,
    )


def test_factory_returns_soniox_adapter():
    adapter = create_stt(_settings())
    assert isinstance(adapter, SonioxAdapter)
    assert adapter.language_hints == ["vi", "en"]


def test_missing_api_key_is_unavailable():
    available, reason = SonioxAdapter(_settings(api_key="")).availability()
    assert not available
    assert "SONIOX_API_KEY" in reason


@pytest.mark.asyncio
async def test_transcribe_keeps_only_final_tokens(tmp_path):
    received: dict = {"audio": b""}

    async def fake_soniox(socket):
        received["config"] = json.loads(await socket.recv())
        async for frame in socket:
            if not frame:
                received["end"] = frame
                break
            received["audio"] += frame
        await socket.send(json.dumps({"tokens": [
            {"text": "Mở", "is_final": True},
            {"text": " cửa", "is_final": False},
        ]}))
        await socket.send(json.dumps({"tokens": [
            {"text": " cửa sổ", "is_final": True},
            {"text": "<fin>", "is_final": True},
        ]}))
        await socket.send(json.dumps({"tokens": [], "finished": True}))

    audio = tmp_path / "turn.webm"
    audio.write_bytes(b"x" * 70_000)
    async with websockets.serve(fake_soniox, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transcript = await SonioxAdapter(_settings(f"ws://127.0.0.1:{port}")).transcribe(audio)

    assert transcript == "Mở cửa sổ"
    assert received["audio"] == b"x" * 70_000
    # Soniox only honours an empty text frame as end of audio.
    assert received["end"] == ""
    assert received["config"]["model"] == "stt-rt-v5"
    assert received["config"]["audio_format"] == "auto"


@pytest.mark.asyncio
async def test_transcribe_raises_on_soniox_error(tmp_path):
    async def fake_soniox(socket):
        await socket.recv()
        await socket.send(json.dumps({"error_code": 401, "error_message": "Invalid API key"}))

    audio = tmp_path / "turn.webm"
    audio.write_bytes(b"x")
    async with websockets.serve(fake_soniox, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(RuntimeError, match="Invalid API key"):
            await SonioxAdapter(_settings(f"ws://127.0.0.1:{port}")).transcribe(audio)


class _FakeStreamingSTT:
    name = "fake-stream"
    device = "cloud"
    dtype = "cloud"
    stream_max_seconds = 5

    def __init__(self):
        self.audio = b""

    def availability(self):
        return True, "ready"

    async def stream(self, audio_chunks, on_update=None):
        async for chunk in audio_chunks:
            self.audio += chunk
            await on_update("Mở", " cửa")
        return "Mở cửa sổ"


def test_stream_route_relays_partials_and_transcript():
    from unittest.mock import patch

    from starlette.testclient import TestClient

    from src.vivi.api.app import app
    from src.vivi.api.runtime import runtime

    fake = _FakeStreamingSTT()
    with patch.object(runtime, "stt", fake):
        client = TestClient(app)
        with client.websocket_connect("/api/v1/stt/stream?session_id=s&turn_id=t") as socket:
            socket.send_bytes(b"abc")
            assert socket.receive_json() == {"type": "partial", "final": "Mở", "interim": " cửa"}
            socket.send_text("stop")
            done = socket.receive_json()
    assert done["type"] == "done"
    assert done["transcript"] == "Mở cửa sổ"
    assert fake.audio == b"abc"


def test_stream_route_reports_unavailable_stt():
    from starlette.testclient import TestClient

    from src.vivi.api.app import app

    client = TestClient(app)
    with client.websocket_connect("/api/v1/stt/stream?session_id=s&turn_id=t") as socket:
        message = socket.receive_json()
    assert message["type"] == "error"
    assert "STT chưa sẵn sàng" in message["detail"]


@pytest.mark.asyncio
async def test_stream_reports_interim_text(tmp_path):
    async def fake_soniox(socket):
        await socket.recv()
        async for frame in socket:
            if not frame:
                break
            await socket.send(json.dumps({"tokens": [
                {"text": "Mở", "is_final": True},
                {"text": " cửa", "is_final": False},
            ]}))
        await socket.send(json.dumps({"tokens": [{"text": " cửa", "is_final": True}], "finished": True}))

    async def chunks():
        yield b"a"

    updates = []

    async def on_update(final, interim):
        updates.append((final, interim))

    async with websockets.serve(fake_soniox, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = SonioxAdapter(_settings(f"ws://127.0.0.1:{port}"))
        transcript = await adapter.stream(chunks(), on_update)

    assert updates[0] == ("Mở", " cửa")
    assert transcript == "Mở cửa"


class _StubSTT:
    device = "stub"
    dtype = "stub"

    def __init__(self, name, available=True, fails=False):
        self.name = name
        self.available = available
        self.fails = fails

    def availability(self):
        return self.available, "ready" if self.available else "missing key"

    async def transcribe(self, path):
        if self.fails:
            raise RuntimeError("network down")
        return f"{self.name} text"


def test_factory_wraps_soniox_with_phowhisper_fallback():
    from src.vivi.speech.stt import FallbackSTTAdapter, PhoWhisperAdapter

    adapter = create_stt(Settings(stt_provider="soniox", stt_fallback_provider="phowhisper"))
    assert isinstance(adapter, FallbackSTTAdapter)
    assert isinstance(adapter.fallback, PhoWhisperAdapter)


@pytest.mark.asyncio
async def test_fallback_used_when_primary_missing_or_failing(tmp_path):
    from src.vivi.speech.stt import FallbackSTTAdapter

    missing = FallbackSTTAdapter(_StubSTT("soniox", available=False), _StubSTT("phowhisper"))
    assert missing.name == "phowhisper (dự phòng)"
    assert missing.streaming is False
    assert await missing.transcribe(tmp_path / "a.webm") == "phowhisper text"

    failing = FallbackSTTAdapter(_StubSTT("soniox", fails=True), _StubSTT("phowhisper"))
    assert failing.name == "soniox"
    assert await failing.transcribe(tmp_path / "a.webm") == "phowhisper text"
