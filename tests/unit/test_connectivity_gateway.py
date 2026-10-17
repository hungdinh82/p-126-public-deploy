from __future__ import annotations

import httpx
import pytest

from jetson_ai.scheduler import InferenceScheduler
from src.vivi.config import Settings
from src.vivi.connectivity import ConnectivitySupervisor
from src.vivi.model_gateway import ModelGateway, ModelProvider
from tests.support.core import ScriptedModel, action


def _config(tmp_path, **values):
    return Settings(
        data_dir=tmp_path,
        jetson_ai_url="http://jetson.test",
        jetson_ai_token="test-token",
        ai_server_auth_required=True,
        rag_local_only=False,
        connectivity_failure_threshold=2,
        connectivity_success_threshold=2,
        **values,
    )


@pytest.mark.asyncio
async def test_gateway_hysteresis_and_routing_matrix(tmp_path):
    state = {"internet": True, "jetson": True}

    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clients3.google.com":
            return httpx.Response(204 if state["internet"] else 503)
        if request.url.host == "jetson.test" and state["jetson"]:
            return httpx.Response(
                200,
                json={
                    "contract_version": "1.0",
                    "capabilities": {name: {"status": "ready"} for name in ("stt", "llm", "tts")},
                },
            )
        return httpx.Response(503)

    supervisor = ConnectivitySupervisor(
        _config(tmp_path, jetson_ai_policy="allow_cloud"), transport=httpx.MockTransport(reply)
    )
    try:
        await supervisor.probe_once()
        gateway = ModelGateway(primary="google", provider_allowed=supervisor.provider_allowed)

        def available():
            return [name for name in gateway.chain() if supervisor.provider_allowed(name)]

        assert supervisor.snapshot()["effective"]["mode"] == "cloud"
        assert available() == ["google", "local"]

        state["internet"] = False
        await supervisor.probe_once()
        assert supervisor.internet == "online"  # first miss is tolerated
        await supervisor.probe_once()
        assert supervisor.internet == "offline"
        assert available() == ["local"]

        supervisor.set_override("both_down")
        assert available() == []
        supervisor.set_override("actual")
        supervisor.set_profile("cloud_only")
        assert available() == []
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_jetson_capability_override_is_independent(tmp_path):
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clients3.google.com":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "contract_version": "1.0",
                "capabilities": {name: {"status": "ready"} for name in ("stt", "llm", "tts")},
            },
        )

    supervisor = ConnectivitySupervisor(_config(tmp_path), transport=httpx.MockTransport(reply))
    try:
        await supervisor.probe_once()
        supervisor.set_override("stt_down")
        assert not supervisor.capability_available("stt", "jetson")
        assert supervisor.capability_available("llm", "jetson")
        assert supervisor.snapshot()["actual"]["jetson_capabilities"]["stt"] == "ready"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_slow_llm_does_not_block_speech_lane():
    scheduler = InferenceScheduler()
    async with await scheduler.acquire("llm"):
        async with await scheduler.acquire("stt", timeout=0.05):
            assert scheduler.snapshot("stt")["active_requests"] == 1
        async with await scheduler.acquire("tts", timeout=0.05):
            assert scheduler.snapshot("tts")["active_requests"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,accepting,expected", [("ready", True, True), ("busy", True, True), ("busy", False, False)]
)
async def test_busy_jetson_is_routable_while_it_accepts_queue(tmp_path, status, accepting, expected):
    def reply(request):
        if request.url.host != "jetson.test":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "contract_version": "1.0",
                "capabilities": {
                    name: {"status": status, "accepting_requests": accepting} for name in ("stt", "llm", "tts")
                },
            },
        )

    monitor = ConnectivitySupervisor(_config(tmp_path), transport=httpx.MockTransport(reply))
    await monitor.probe_once()
    assert monitor.capability_available("llm", "jetson") is expected
    assert monitor.capability_available("stt", "jetson") is expected


@pytest.mark.asyncio
async def test_cloud_timeout_uses_local_within_shared_semantic_budget(tmp_path):
    import asyncio

    from src.vivi.dialogue.contracts import SessionState
    from src.vivi.dialogue.planner import SemanticResolver

    class Cloud:
        async def agenerate_json(self, **kwargs):
            await asyncio.sleep(1)

    local = ScriptedModel([action("climate.set_temperature", {"temperature_delta_celsius": 2})])
    monitor = ConnectivitySupervisor(_config(tmp_path))
    resolver = SemanticResolver(
        gateway=ModelGateway(
            {"google": ModelProvider("google", Cloud(), 0.02), "local": ModelProvider("local", local, 0.3)},
            provider_allowed=monitor.provider_allowed,
            result_recorder=monitor.record_provider_result,
        ),
        budget_seconds=0.2,
    )
    frame, served, attempts = await resolver.resolve("tôi lạnh quá", SessionState(), requested="google")
    assert served == "local" and len(local.calls) == 1
    assert attempts[0]["reason"] == "TimeoutError"
    assert attempts[1]["budget_seconds"] < 0.2
    assert frame.actions[0].arguments.temperature_delta_celsius == 2
    assert [row["provider"] for row in monitor.store.recent() if row["kind"] == "provider_result"] == [
        "local",
        "google",
    ]
    monitor.store.close()


@pytest.mark.parametrize(
    "profile,chain",
    [("auto", ["google", "local"]), ("cloud_only", ["google"]), ("jetson_only", ["local"]), ("fallback_only", [])],
)
def test_profiles_apply_to_explicit_preferences_and_auto(tmp_path, profile, chain):
    monitor = ConnectivitySupervisor(_config(tmp_path))
    monitor.internet = monitor.jetson = "online"
    monitor.capabilities = {name: "ready" for name in ("stt", "llm", "tts")}
    monitor.set_profile(profile)
    gateway = ModelGateway(provider_allowed=monitor.provider_allowed)
    assert [name for name in gateway.chain() if monitor.provider_allowed(name)] == chain
    if profile == "auto":
        assert gateway.chain("local") == ["local"]
    monitor.store.close()


def test_offline_only_never_routes_cloud_even_with_internet(tmp_path):
    monitor = ConnectivitySupervisor(_config(tmp_path, jetson_ai_policy="offline_only"))
    monitor.internet = monitor.jetson = "online"
    monitor.capabilities = {name: "ready" for name in ("stt", "llm", "tts")}
    gateway = ModelGateway(offline=True, provider_allowed=monitor.provider_allowed)
    assert [name for name in gateway.chain("google") if monitor.provider_allowed(name)] == ["local"]
    assert not monitor.capability_available("stt", "cloud")
    monitor.store.close()


def test_no_auth_jetson_uses_remote_speech_without_loading_pc_models(tmp_path):
    from src.vivi.speech.jetson import JetsonSTTAdapter
    from src.vivi.speech.stt import create_stt
    from src.vivi.speech.tts import JetsonTTSAdapter, create_tts, create_tts_engines

    config = _config(tmp_path).model_copy(
        update={
            "ai_server_auth_required": False,
            "jetson_ai_token": "",
            "jetson_ai_policy": "prefer_local",
            "stt_provider": "soniox",
            "tts_provider": "zerotts",
        }
    )
    monitor = ConnectivitySupervisor(config)
    stt = create_stt(config, monitor)
    assert isinstance(stt.fallback, JetsonSTTAdapter)
    default = create_tts(config)
    assert isinstance(default, JetsonTTSAdapter)
    assert create_tts_engines(config, default, monitor)["zerotts"] is default
    monitor.store.close()


@pytest.mark.asyncio
async def test_failed_probe_can_use_configured_provider_reachability(tmp_path):
    config = _config(tmp_path, google_api_key="test-only")

    def reply(request):
        return httpx.Response(401 if request.url.host == "generativelanguage.googleapis.com" else 503)

    monitor = ConnectivitySupervisor(config, transport=httpx.MockTransport(reply))
    await monitor.probe_once()
    assert monitor.internet == "online"
    assert monitor.jetson != "online"
    monitor.store.close()


@pytest.mark.asyncio
async def test_stt_failover_keeps_audio_and_reports_actual_provider(tmp_path):
    from src.vivi.speech.stt import AdaptiveSTTAdapter

    class Engine:
        device = dtype = "fake"

        def __init__(self, name):
            self.name = name

        def availability(self):
            return True, "ready"

        async def transcribe(self, path):
            assert path == tmp_path / "original.wav"
            if self.name == "soniox":
                raise TimeoutError()
            return "lời nói được giữ lại"

    monitor = ConnectivitySupervisor(_config(tmp_path))
    monitor.internet = monitor.jetson = "online"
    monitor.capabilities = {name: "ready" for name in ("stt", "llm", "tts")}
    monitor._has_observation = True
    adapter = AdaptiveSTTAdapter(Engine("soniox"), Engine("jetson"), monitor)
    assert await adapter.transcribe(tmp_path / "original.wav") == "lời nói được giữ lại"
    assert adapter.name == "jetson"
    monitor.store.close()


@pytest.mark.asyncio
async def test_local_queue_pressure_uses_short_breaker_not_schema_failure_breaker():
    import time

    from src.vivi.dialogue.contracts import SessionState
    from src.vivi.dialogue.planner import ModelProvider, SemanticResolver

    class Busy:
        async def agenerate_tool(self, **kwargs):
            request = httpx.Request("POST", "http://jetson.test/v1/chat/completions")
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError("busy", request=request, response=response)

    provider = ModelProvider("local", Busy(), 1)
    resolver = SemanticResolver({"local": provider}, breaker_seconds=30)
    _, served, attempts = await resolver.resolve("tôi lạnh quá", SessionState(), requested="local")
    assert served is None and attempts[0]["http_status"] == 429
    assert 0 < provider.unavailable_until - time.monotonic() <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["semantic", "destination_choice", "conversation_answer", "handbook_answer"])
async def test_connectivity_filters_all_stages_without_rerouting_or_renewing_budgets(tmp_path, stage):
    monitor = ConnectivitySupervisor(_config(tmp_path, jetson_ai_policy="allow_cloud"))
    monitor.internet = monitor.jetson = "online"
    monitor.capabilities = {name: "ready" for name in ("stt", "llm", "tts")}
    monitor.set_override("internet_down")
    gateway = ModelGateway(
        {name: ModelProvider(name, object()) for name in ("google", "openai", "local")},
        provider_allowed=monitor.provider_allowed,
    )
    calls = []

    async def serve(provider, attempts):
        calls.append(provider.name)
        return "valid"

    result = await gateway.run(stage, serve, requested="google")
    assert result.provider == "local" and calls == ["local"]
    assert result.attempts[0]["outcome"] == "unavailable" and result.attempts[0]["calls"] == 0
    monitor.set_override("actual")
    monitor.set_profile("fallback_only")
    result = await gateway.run(stage, serve, requested="google")
    assert result.provider is None and result.value is None and calls == ["local"]
    assert all(a["calls"] == 0 for a in result.attempts)
    monitor.store.close()


def test_runtime_stt_availability_reads_background_probe_without_sync_http(tmp_path, monkeypatch):
    from src.vivi.speech.stt import create_stt

    config = _config(tmp_path, jetson_ai_policy="offline_only", stt_provider="soniox", stt_fallback_provider="off")
    monitor = ConnectivitySupervisor(config)
    monitor.jetson = "online"
    monitor.capabilities["stt"] = "ready"
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ffmpeg")

    def forbidden(*args, **kwargs):
        raise AssertionError("Health must not block the event loop on a synchronous HTTP probe")

    monkeypatch.setattr(httpx, "Client", forbidden)
    stt = create_stt(config, monitor)
    assert stt.availability()[0] and stt.name == "jetson"
    monitor.set_override("stt_down")
    assert not stt.availability()[0]
    monitor.store.close()


@pytest.mark.asyncio
async def test_offline_map_rejects_new_work_without_touching_stored_route(tmp_path):
    from src.vivi.navigation.connectivity import ConnectivityMapClient
    from src.vivi.navigation.vietmap import VietMapError
    from tests.support.core import Maps

    monitor = ConnectivitySupervisor(_config(tmp_path))
    monitor.set_override("internet_down")
    maps = Maps()
    client = ConnectivityMapClient(maps, monitor)
    with pytest.raises(VietMapError):
        await client.route((21, 105), (22, 106))
    with pytest.raises(VietMapError):
        await client.search("Tràng An")
    with pytest.raises(VietMapError):
        await client.matrix([(21, 105), (22, 106)])
    assert not maps.routes and not maps.queries
    monitor.set_override("actual")
    assert await client.search("Tràng An")
    assert maps.queries == ["Tràng An"]
    monitor.store.close()
