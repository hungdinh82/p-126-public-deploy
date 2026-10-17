from __future__ import annotations

import json
import struct

import httpx
import pytest

from src.vivi.config import Settings
from src.vivi.speech.tts import VieNeuTTSAdapter, create_tts


def _frame(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _settings(**overrides) -> Settings:
    return Settings(**{"tts_provider": "vieneu", "vieneu_voice": "Mai Bé Phương", **overrides})


def _adapter(handler, api_key: str = "vn_test_key") -> VieNeuTTSAdapter:
    adapter = VieNeuTTSAdapter(_settings(vieneu_api_key=api_key))
    adapter.transport = httpx.MockTransport(handler)
    return adapter


def test_factory_defaults_to_mai_be_phuong():
    assert Settings.model_fields["vieneu_voice"].default == "Mai Bé Phương"
    adapter = create_tts(_settings(vieneu_api_key="k"))
    assert isinstance(adapter, VieNeuTTSAdapter)
    assert adapter.voice == "Mai Bé Phương"
    assert adapter.sample_rate == 24000
    assert adapter.availability()[0]


def test_missing_api_key_is_unavailable():
    available, reason = VieNeuTTSAdapter(Settings(vieneu_api_key="")).availability()
    assert not available
    assert "VIENEU_API_KEY" in reason


@pytest.mark.asyncio
async def test_stream_unframes_pcm_and_keeps_whole_samples():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        # An odd-length frame must not split a 16-bit sample.
        body = _frame(b"\x01\x02\x03") + _frame(b"\x04\x05\x06") + _frame(b"")
        return httpx.Response(200, content=body)

    chunks = [chunk async for chunk in _adapter(handler).stream("Đã mở cốp")]

    assert chunks == [b"\x01\x02", b"\x03\x04\x05\x06"]
    assert seen["url"] == "https://api.vieneu.io/api/v1/tts/stream"
    assert seen["auth"] == "Bearer vn_test_key"
    assert seen["body"] == {
        "text": "Đã mở cốp",
        "voiceId": "Mai Bé Phương",
        "outputFormat": "pcm",
        "sampleRate": 24000,
    }


@pytest.mark.asyncio
async def test_synthesize_wraps_pcm_in_wav():
    def handler(request):
        return httpx.Response(200, content=_frame(b"\x00\x00\xff\x7f") + _frame(b""))

    audio = await _adapter(handler).synthesize("Xin chào")
    assert audio[:4] == b"RIFF"
    assert audio.endswith(b"\x00\x00\xff\x7f")


@pytest.mark.asyncio
async def test_stream_raises_on_api_error_and_truncation():
    def busy(request):
        return httpx.Response(503, json={"code": "STREAM_BUSY"})

    with pytest.raises(RuntimeError, match="VieNeu 503: .*STREAM_BUSY"):
        [chunk async for chunk in _adapter(busy).stream("Xin chào")]

    def cut(request):
        return httpx.Response(200, content=_frame(b"\x00\x00"))

    with pytest.raises(RuntimeError, match="ngắt stream"):
        [chunk async for chunk in _adapter(cut).stream("Xin chào")]


def test_engines_reuse_default_and_offer_both_voices():
    from src.vivi.speech.tts import ZeroTTSAdapter, create_tts_engines

    config = Settings(tts_provider="vieneu", vieneu_api_key="k")
    default = create_tts(config)
    engines = create_tts_engines(config, default)
    assert engines["vieneu"] is default
    assert isinstance(engines["zerotts"], ZeroTTSAdapter)


def test_stream_route_uses_engine_picked_in_settings():
    from unittest.mock import patch

    from starlette.testclient import TestClient

    from src.vivi.api.app import app
    from src.vivi.api.runtime import runtime

    class FakeTTS:
        name = "vieneu"
        device = "cloud"
        execution_providers: list[str] = []
        sample_rate = 24000

        def __init__(self, voice, pcm):
            self.voice = voice
            self.pcm = pcm

        def availability(self):
            return True, "ready"

        async def stream(self, text):
            yield self.pcm

    local = FakeTTS("VIVI", b"\x01\x00")
    cloud = FakeTTS("Mai Bé Phương", b"\x02\x00")
    body = {"text": "Xin chào", "session_id": "s", "turn_id": "t"}
    with patch.object(runtime, "tts", cloud), \
         patch.object(runtime, "tts_engines", {"zerotts": local, "vieneu": cloud}):
        client = TestClient(app)
        default = client.post("/api/v1/tts/stream", json=body)
        picked = client.post("/api/v1/tts/stream", json={**body, "tts_provider": "zerotts"})
        unknown = client.post("/api/v1/tts/stream", json={**body, "tts_provider": "edge"})
        options = client.get("/api/v1/health").json()["tts"]["options"]

    assert default.content == b"\x02\x00"
    assert picked.content == b"\x01\x00"
    assert picked.headers["X-ViVi-Voice"] == "VIVI"
    assert unknown.status_code == 422
    assert [item["provider"] for item in options] == ["zerotts", "vieneu"]
