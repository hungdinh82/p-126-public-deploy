"""Network changes must not become semantic edits or execution consent."""

import pytest

from src.vivi.config import Settings
from src.vivi.connectivity import ConnectivitySupervisor
from tests.support.core import Harness, action


@pytest.mark.asyncio
async def test_lost_models_preserve_question_and_typed_consent_still_works(tmp_path):
    h = Harness(tmp_path, [action("navigation.plan", {"query": "Tràng An", "purpose": "navigate"})])
    monitor = ConnectivitySupervisor(Settings(data_dir=tmp_path, jetson_ai_policy="disabled"))
    h.models.provider_allowed = monitor.provider_allowed
    try:
        preview = await h.turn("đến Tràng An")
        question = preview["pending_question"]
        plan = preview["navigation_workspace"]["draft"]
        calls = len(h.model.calls)
        monitor.set_profile("fallback_only")
        unanswered = await h.turn("đúng, bắt đầu luôn rồi đổi sang điểm khác")
        assert unanswered["pending_question"]["id"] == question["id"]
        assert unanswered["navigation_workspace"]["draft"]["revision"] == plan["revision"]
        assert len(h.model.calls) == calls
        assert not h.vehicle.state_for("driver").driving
        confirmed = await h.turn(question_reply={"question_id": question["id"], "option_id": "yes"})
        assert confirmed["navigation_workspace"]["journey"]["status"] == "running"
        assert h.vehicle.state_for("driver").driving
        assert len(h.model.calls) == calls
        with h.ledger.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 1
    finally:
        await h.close()
        monitor.store.close()


@pytest.mark.asyncio
async def test_tts_first_chunk_failure_falls_back_without_replaying_turn(client, monkeypatch):
    from src.vivi.api.runtime import active_runtime

    runtime = active_runtime.get()

    calls = []

    class Engine:
        device = "jetson"
        execution_providers = []
        sample_rate = 24000

        def __init__(self, name):
            self.name = self.voice = name

        def availability(self):
            return True, "ready"

        async def stream(self, text):
            calls.append((self.name, text))
            if self.name == "vieneu":
                raise TimeoutError()
            yield b"\x01\x00"

    cloud, local = Engine("vieneu"), Engine("zerotts")
    monkeypatch.setattr(runtime, "tts", cloud)
    monkeypatch.setattr(runtime, "tts_engines", {"vieneu": cloud, "zerotts": local})
    response = await client.post(
        "/api/v1/tts/stream",
        json={
            "text": "Đã hoàn thành",
            "session_id": "tts-fallback-test",
            "turn_id": "tts-only",
            "tts_provider": "auto",
        },
    )
    assert response.status_code == 200 and response.content == b"\x01\x00"
    assert response.headers["X-ViVi-Voice"] == "zerotts"
    assert len(calls) == 2 and calls[0][1] == calls[1][1]


@pytest.mark.asyncio
async def test_engineer_profile_filters_shared_gateway_and_keeps_v2_inputs(client):
    from src.vivi.api.runtime import active_runtime

    runtime = active_runtime.get()
    assert (await client.post("/api/v1/engineer/models/apply", json={"profile_id": "fallback_only"})).status_code == 403
    await client.post("/api/v1/session/role", json={"role": "engineer"})
    response = await client.post("/api/v1/engineer/models/apply", json={"profile_id": "fallback_only"})
    assert response.status_code == 200 and response.json()["changed_runtime"]
    health = (await client.get("/api/v1/health")).json()
    assert health["orchestration"]["workflow_version"] == "core-2"
    assert health["connectivity"]["effective"]["mode"] == "fallback"
    assert all(item["provider"] not in {"auto", "rules"} for item in health["llm"]["options"])
    gateway = runtime.orchestrator.graph.navigation.workflow.resolver.gateway
    assert not gateway.provider_allowed("google") and not gateway.provider_allowed("local")
    for provider in ("auto", "rules"):
        response = await client.post(
            "/api/v2/turn",
            json={
                "session_id": "filtered",
                "turn_id": provider,
                "input": {"kind": "utterance", "text": "bật nhạc"},
                "provider": provider,
            },
        )
        assert response.status_code == 422
        assert any(error["loc"][-1] == "provider" for error in response.json()["detail"])
    scenario = await client.post("/api/v1/engineer/connectivity/scenario", json={"scenario": "both_down"})
    assert scenario.status_code == 200 and scenario.json()["simulated"]
    assert (await client.post("/api/v1/turn", json={})).status_code == 404


@pytest.mark.asyncio
async def test_cloud_only_speech_does_not_fallback_to_device_and_poll_does_not_probe(client, monkeypatch):
    from src.vivi.api.runtime import active_runtime

    runtime = active_runtime.get()
    calls = []

    class Engine:
        device = "cpu"
        sample_rate = 24000
        name = voice = "zerotts"

        def availability(self):
            return True, "ready"

        async def stream(self, text):
            calls.append(text)
            yield b"\x01\x00"

    monkeypatch.setattr(runtime, "tts_engines", {"zerotts": Engine()})
    runtime.connectivity.set_profile("cloud_only")
    response = await client.post(
        "/api/v1/tts/stream",
        json={"text": "Xin chào", "session_id": "speech-profile", "turn_id": "one", "tts_provider": "zerotts"},
    )
    assert response.status_code == 503 and not calls

    def forbidden():
        raise AssertionError("Connectivity poll must only read the background snapshot")

    monkeypatch.setattr(runtime.stt, "availability", forbidden)
    monkeypatch.setattr(runtime.tts, "availability", forbidden)
    response = await client.get("/api/v1/connectivity")
    assert response.status_code == 200 and response.json()["profile"] == "cloud_only"
