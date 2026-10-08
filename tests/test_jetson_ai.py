from __future__ import annotations

import httpx
import pytest

from jetson_ai.app import ServiceConfig, create_app


@pytest.fixture
def fake_backend():
    calls = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "xin chào"}}]})
        return httpx.Response(404)

    return calls, httpx.MockTransport(reply)


@pytest.mark.anyio
async def test_private_gateway_contract_and_legacy_chat(fake_backend):
    calls, backend = fake_backend
    config = ServiceConfig(token="a" * 32)
    app = create_app(config, transport=backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/v1/capabilities")).status_code == 401
        headers = {"Authorization": "Bearer " + config.token}
        cap = (await client.get("/v1/capabilities", headers=headers)).json()
        assert cap["capabilities"]["llm"]["status"] == "ready"
        assert (await client.get("/v1/models", headers=headers)).json()["data"][0]["id"] == config.model_id
        assert cap["capabilities"]["stt"]["status"] == "unavailable"
        assert cap["capabilities"]["tts"]["status"] == "unavailable"
        payload = {"messages": [{"role": "user", "content": "xin chào"}], "max_tokens": 32}
        chat = await client.post("/v1/chat/completions", headers=headers, json=payload)
        assert chat.status_code == 200
        assert chat.json()["choices"][0]["message"]["content"] == "xin chào"
        canonical = await client.post("/v1/llm/generate", headers=headers, json={
            "contract_version": "1.0", "request_id": "req-1", "call_id": "call-1",
            "attempt": 1, "capability": "llm", "input": payload,
        })
        assert canonical.status_code == 200
        assert canonical.json()["result"]["text"] == chat.json()["choices"][0]["message"]["content"]
        assert canonical.json()["call_id"] == "call-1"
        assert len([c for c in calls if c.url.path == "/v1/chat/completions"]) == 2


@pytest.mark.anyio
async def test_private_gateway_rejects_invalid_input_and_reports_unavailable(fake_backend):
    _calls, backend = fake_backend
    config = ServiceConfig(token="b" * 32)
    app = create_app(config, transport=backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + config.token}
        bad = await client.post("/v1/chat/completions", headers=headers, json={"messages": [], "max_tokens": 1})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "INVALID_REQUEST"
        old = await client.post("/v1/llm/generate", headers=headers, json={"contract_version": "2.0", "capability": "llm"})
        assert old.status_code == 400
        assert (await client.post("/v1/stt/transcriptions", headers=headers)).status_code == 503
        assert (await client.post("/v1/tts/synthesize", headers=headers)).status_code == 503


@pytest.mark.anyio
async def test_model_readiness_and_timeout_are_separate_from_process_liveness():
    config = ServiceConfig(token="c" * 32)
    state = {"ready": False}

    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200 if state["ready"] else 503,
                                  json={"status": "ok" if state["ready"] else "loading"})
        raise httpx.ReadTimeout("fake model timeout")

    app = create_app(config, transport=httpx.MockTransport(reply))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + config.token}
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/v1/capabilities", headers=headers)).json()["capabilities"]["llm"]["status"] == "loading"
        payload = {"messages": [{"role": "user", "content": "hello"}]}
        down = await client.post("/v1/chat/completions", headers=headers, json=payload)
        assert down.status_code == 503
        assert down.json()["error"]["code"] == "MODEL_LOADING"
        state["ready"] = True
        timed_out = await client.post("/v1/chat/completions", headers=headers, json=payload)
        assert timed_out.status_code == 504
        assert timed_out.json()["error"]["code"] == "SERVICE_TIMEOUT"


@pytest.mark.anyio
async def test_test_ui_bootstraps_token_only_for_local_browser(fake_backend):
    _, backend = fake_backend
    config = ServiceConfig(token="secret-test-token-" * 3)
    app = create_app(config, transport=backend)
    for host, base, expected in (("127.0.0.1", "http://localhost", True),
                                 ("192.168.1.20", "http://localhost", False),
                                 ("127.0.0.1", "http://external.example", False)):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 2000)),
                                     base_url=base) as client:
            response = await client.get("/test")
            assert response.status_code == 200
            assert (config.token in response.text) == expected
            assert response.headers["cache-control"] == "no-store"
            assert (await client.get("/v1/capabilities")).status_code == 401


@pytest.mark.anyio
async def test_ui_cannot_inject_configured_token_into_html(fake_backend):
    _, backend = fake_backend
    config = ServiceConfig(token="a" * 32 + "</script><script>alert(1)</script>")
    app = create_app(config, transport=backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/")
        assert config.token not in response.text
        assert "\\u003c/script>" in response.text


@pytest.mark.anyio
async def test_speech_api_validates_wav_and_returns_audio_and_timings(fake_backend):
    import io
    import wave

    _, backend = fake_backend
    config = ServiceConfig(token="s" * 32)
    app = create_app(config, transport=backend)
    speech = app.state.speech
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)
    audio = output.getvalue()
    for name in ("stt", "tts"):
        speech.info[name].update(status="ready", sample_rate=16000)
    speech.transcribe = lambda data: "Xin chào" if data == audio else ""
    speech.synthesize = lambda text: audio
    headers = {"Authorization": "Bearer " + config.token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers=headers) as client:
        stt = await client.post("/v1/stt/transcriptions", files={"file": ("voice.wav", audio, "audio/wav")})
        assert stt.status_code == 200
        assert stt.json()["text"] == "Xin chào"
        assert stt.json()["latency_ms"] >= 0
        invalid = await client.post("/v1/stt/transcriptions", files={"file": ("bad.wav", b"bad", "audio/wav")})
        assert invalid.status_code == 400
        tts = await client.post("/v1/tts/synthesize", json={"text": "Xin chào"})
        assert tts.status_code == 200
        assert tts.content == audio
        assert tts.headers["content-type"] == "audio/wav"
        assert int(tts.headers["x-vivi-latency-ms"]) >= 0
        assert (await client.post("/v1/tts/synthesize", json={"text": " "})).status_code == 400


@pytest.mark.anyio
async def test_gpu_status_requires_actual_loaded_backend(fake_backend, tmp_path):
    _, backend = fake_backend
    log = tmp_path / "llama.log"
    log.write_text("Device 0: Test GPU\noffloaded 12/29 layers to GPU\n")
    app = create_app(ServiceConfig(token="g" * 32, llm_log_path=str(log)), transport=backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer " + "g" * 32}) as client:
        info = (await client.get("/v1/capabilities")).json()["capabilities"]
        assert info["llm"]["using_gpu"] is True
        assert info["llm"]["gpu_layers"] == 12
        assert info["stt"]["using_gpu"] is False
        assert info["tts"]["using_gpu"] is False
        log.write_text("CUDA configured, no model loaded")
        info = (await client.get("/v1/capabilities")).json()["capabilities"]
        assert info["llm"]["using_gpu"] is False
