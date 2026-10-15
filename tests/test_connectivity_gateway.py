from __future__ import annotations

import httpx
import pytest

from jetson_ai.scheduler import InferenceScheduler
from src.vivi.config import Settings
from src.vivi.connectivity import ConnectivitySupervisor


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
            return httpx.Response(200, json={
                "contract_version": "1.0",
                "capabilities": {name: {"status": "ready"} for name in ("stt", "llm", "tts")},
            })
        return httpx.Response(503)

    supervisor = ConnectivitySupervisor(_config(tmp_path), transport=httpx.MockTransport(reply))
    try:
        await supervisor.probe_once()
        available = {"rules", "local", "google", "openai"}
        assert supervisor.snapshot()["effective"]["mode"] == "cloud"
        assert supervisor.llm_chain("auto", available)[:2] == ["google", "openai"]

        state["internet"] = False
        await supervisor.probe_once()
        assert supervisor.internet == "online"  # first miss is tolerated
        await supervisor.probe_once()
        assert supervisor.internet == "offline"
        assert supervisor.llm_chain("auto", available) == ["local"]

        supervisor.set_override("both_down")
        assert await supervisor.select_llm("auto", available) == "rules"
        supervisor.set_override("actual")
        supervisor.set_profile("cloud_only")
        assert await supervisor.select_llm("auto", available) == "rules"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_jetson_capability_override_is_independent(tmp_path):
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clients3.google.com":
            return httpx.Response(204)
        return httpx.Response(200, json={
            "contract_version": "1.0",
            "capabilities": {name: {"status": "ready"} for name in ("stt", "llm", "tts")},
        })

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
    async with await scheduler.acquire('llm'):
        async with await scheduler.acquire('stt', timeout=.05):
            assert scheduler.snapshot('stt')['active_requests'] == 1
        async with await scheduler.acquire('tts', timeout=.05):
            assert scheduler.snapshot('tts')['active_requests'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,accepting,expected', [('ready', True, True), ('busy', True, True), ('busy', False, False)])
async def test_busy_jetson_is_routable_while_it_accepts_queue(tmp_path, status, accepting, expected):
    def reply(request):
        if request.url.host != 'jetson.test':
            return httpx.Response(204)
        return httpx.Response(200, json={'contract_version': '1.0', 'capabilities': {
            name: {'status': status, 'accepting_requests': accepting} for name in ('stt', 'llm', 'tts')}})
    monitor = ConnectivitySupervisor(_config(tmp_path), transport=httpx.MockTransport(reply))
    await monitor.probe_once()
    assert monitor.capability_available('llm', 'jetson') is expected
    assert monitor.capability_available('stt', 'jetson') is expected


@pytest.mark.asyncio
async def test_cloud_timeout_preserves_separate_local_budget_and_native_tools(tmp_path):
    import asyncio

    from src.vivi.dialogue.contracts import SessionState
    from src.vivi.dialogue.planner import ModelProvider, SemanticResolver

    class Cloud:
        async def agenerate_json(self, **kwargs):
            await asyncio.sleep(1)

    class Local:
        async def agenerate_tools(self, **kwargs):
            assert all(t['function']['name'] != 'interpret_turn' for t in kwargs['tools'])
            await asyncio.sleep(.05)
            return [('climate_adjust_temperature', {'direction': 'increase'})]

    resolver = SemanticResolver({'google': ModelProvider('google', Cloud(), .02),
                                 'local': ModelProvider('local', Local(), .3)},
                                budget_seconds=.02, chain_provider=lambda _: ['google', 'local'])
    frame, served, attempts = await resolver.resolve('tôi lạnh quá', SessionState(), requested='google')
    assert served == 'local'
    assert attempts[0]['reason'] == 'TimeoutError'
    assert attempts[1]['semantic_contract'] == 'local-proposals-v2'
    assert frame.actions[0].arguments.temperature_delta_celsius == 2


@pytest.mark.parametrize('profile,chain', [('auto', ['google', 'local']), ('cloud_only', ['google']),
                                         ('jetson_only', ['local']), ('rules_only', [])])
def test_profiles_apply_to_explicit_preferences_and_auto(tmp_path, profile, chain):
    monitor = ConnectivitySupervisor(_config(tmp_path))
    monitor.internet = monitor.jetson = 'online'
    monitor.capabilities = {name: 'ready' for name in ('stt', 'llm', 'tts')}
    monitor.set_profile(profile)
    assert monitor.llm_chain('auto', {'google', 'local'}) == chain
    if profile == 'auto':
        assert monitor.llm_chain('local', {'google', 'local'}) == ['local', 'google']
    monitor.store.close()


def test_offline_only_never_routes_cloud_even_with_internet(tmp_path):
    monitor = ConnectivitySupervisor(_config(tmp_path, jetson_ai_policy='offline_only'))
    monitor.internet = monitor.jetson = 'online'
    monitor.capabilities = {name: 'ready' for name in ('stt', 'llm', 'tts')}
    assert monitor.llm_chain('google', {'google', 'local'}) == ['local']
    assert not monitor.capability_available('stt', 'cloud')
    monitor.store.close()


def test_no_auth_jetson_uses_remote_speech_without_loading_pc_models(tmp_path):
    from src.vivi.speech.jetson import JetsonSTTAdapter
    from src.vivi.speech.stt import create_stt
    from src.vivi.speech.tts import JetsonTTSAdapter, create_tts, create_tts_engines

    config = _config(tmp_path).model_copy(update={'ai_server_auth_required': False, 'jetson_ai_token': '',
        'jetson_ai_policy': 'prefer_local', 'stt_provider': 'soniox', 'tts_provider': 'zerotts'})
    monitor = ConnectivitySupervisor(config)
    stt = create_stt(config, monitor)
    assert isinstance(stt.fallback, JetsonSTTAdapter)
    default = create_tts(config)
    assert isinstance(default, JetsonTTSAdapter)
    assert create_tts_engines(config, default, monitor)['zerotts'] is default
    monitor.store.close()


@pytest.mark.asyncio
async def test_failed_probe_can_use_configured_provider_reachability(tmp_path):
    config = _config(tmp_path, google_api_key='test-only')
    def reply(request):
        return httpx.Response(401 if request.url.host == 'generativelanguage.googleapis.com' else 503)
    monitor = ConnectivitySupervisor(config, transport=httpx.MockTransport(reply))
    await monitor.probe_once()
    assert monitor.internet == 'online'
    assert monitor.jetson != 'online'
    monitor.store.close()


@pytest.mark.asyncio
async def test_stt_failover_keeps_audio_and_reports_actual_provider(tmp_path):
    from src.vivi.speech.stt import AdaptiveSTTAdapter

    class Engine:
        device = dtype = 'fake'
        def __init__(self, name): self.name = name
        def availability(self): return True, 'ready'
        async def transcribe(self, path):
            assert path == tmp_path / 'original.wav'
            if self.name == 'soniox':
                raise TimeoutError()
            return 'lời nói được giữ lại'
    monitor = ConnectivitySupervisor(_config(tmp_path))
    monitor.internet = monitor.jetson = 'online'
    monitor.capabilities = {name: 'ready' for name in ('stt', 'llm', 'tts')}
    monitor._has_observation = True
    adapter = AdaptiveSTTAdapter(Engine('soniox'), Engine('jetson'), monitor)
    assert await adapter.transcribe(tmp_path / 'original.wav') == 'lời nói được giữ lại'
    assert adapter.name == 'jetson'
    monitor.store.close()


@pytest.mark.asyncio
async def test_tts_first_chunk_failure_falls_back_without_replaying_turn(client, monkeypatch):
    from src.vivi.api.runtime import runtime

    calls = []
    class Engine:
        device = 'jetson'
        execution_providers = []
        sample_rate = 24000
        def __init__(self, name):
            self.name = self.voice = name
        def availability(self):
            return True, 'ready'
        async def stream(self, text):
            calls.append((self.name, text))
            if self.name == 'vieneu':
                raise TimeoutError()
            yield b'\x01\x00'
    cloud, local = Engine('vieneu'), Engine('zerotts')
    monkeypatch.setattr(runtime, 'tts', cloud)
    monkeypatch.setattr(runtime, 'tts_engines', {'vieneu': cloud, 'zerotts': local})
    response = await client.post('/api/v1/tts/stream', json={
        'text': 'Đã hoàn thành', 'session_id': 'tts-fallback-test', 'turn_id': 'tts-only', 'tts_provider': 'auto'})
    assert response.status_code == 200 and response.content == b'\x01\x00'
    assert response.headers['X-ViVi-Voice'] == 'zerotts'
    assert len(calls) == 2 and calls[0][1] == calls[1][1]


@pytest.mark.asyncio
async def test_local_queue_pressure_uses_short_breaker_not_schema_failure_breaker():
    import time

    from src.vivi.dialogue.contracts import SessionState
    from src.vivi.dialogue.planner import ModelProvider, SemanticResolver

    class Busy:
        async def agenerate_tools(self, **kwargs):
            request = httpx.Request('POST', 'http://jetson.test/v1/chat/completions')
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError('busy', request=request, response=response)
    provider = ModelProvider('local', Busy(), 1)
    resolver = SemanticResolver({'local': provider}, breaker_seconds=30, chain_provider=lambda _: ['local'])
    _, served, attempts = await resolver.resolve('tôi lạnh quá', SessionState(), requested='local')
    assert served == 'rules' and attempts[0]['http_status'] == 429
    assert 0 < provider.unavailable_until - time.monotonic() <= 1


@pytest.mark.asyncio
async def test_pending_slot_survives_provider_change_after_network_recovery():
    from src.vivi.dialogue.planner import SemanticResolver
    from tests.test_local_pending_turns import pending_door

    frame, served, _ = await SemanticResolver().resolve('phía sau', pending_door(), requested='google')
    assert served == 'rules' and frame.act == 'modify'
    assert frame.actions[0].arguments.target_side == 'right'
    assert frame.actions[0].arguments.target_row == 'rear'
    assert frame.actions[0].arguments.open is True
