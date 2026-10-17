from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.vivi.api.runtime import runtime


@pytest.mark.asyncio
async def test_health_uses_canonical_edge_app(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["orchestration"]["engine"] == "langgraph"
    assert payload["orchestration"]["retrieval"] == "sqlite_fts5"


@pytest.mark.asyncio
async def test_edge_metrics_is_available_without_enabling_transcript_storage(client):
    response = await client.get("/api/v1/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert "memory" in payload
    assert "gpu" in payload
    assert payload["pipeline"]["status"] == "chưa có lượt chạy"


@pytest.mark.asyncio
async def test_turn_rejects_empty_transcript(client):
    response = await client.post(
        "/api/v1/turn",
        json={"transcript": "", "session_id": "api", "turn_id": "empty"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_rules_turn_returns_canonical_envelope(client):
    response = await client.post(
        "/api/v1/turn",
        json={
            "transcript": "Đặt nhiệt độ 25 độ",
            "session_id": "api-contract",
            "turn_id": "temperature",
            "llm_provider": "rules",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "vehicle"
    assert payload["status"] == "verified"
    assert payload["action"]["intent"] == "climate.set_temperature"
    assert payload["vehicle_state"]["temperature_celsius"] == 25


@pytest.mark.asyncio
async def test_confirmation_has_one_dedicated_endpoint(client):
    proposed = await client.post(
        "/api/v1/turn",
        json={
            "transcript": "Mở cửa sổ bên tài",
            "session_id": "api-confirm",
            "turn_id": "proposal",
            "llm_provider": "rules",
        },
    )
    assert proposed.status_code == 200
    preview = proposed.json()
    assert preview["status"] == "confirmation_required"
    confirmation_id = preview["confirmation"]["confirmation_id"]

    approved = await client.post(
        f"/api/v1/confirmations/{confirmation_id}",
        json={
            "session_id": "api-confirm",
            "turn_id": "approval",
            "decision": "approve",
            "llm_provider": "rules",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "verified"
    assert approved.json()["vehicle_state"]["window_driver_percent"] == 100


@pytest.mark.asyncio
async def test_demo_driving_state_reaches_safety_gateway(client):
    fixture = await client.put(
        "/api/v1/demo/vehicle/driving",
        json={"session_id": "api-driving", "driving": True},
    )
    assert fixture.status_code == 200
    assert fixture.json()["driving"] is True

    response = await client.post(
        "/api/v1/turn",
        json={
            "transcript": "Mở cửa sổ bên tài",
            "session_id": "api-driving",
            "turn_id": "window-while-driving",
            "llm_provider": "rules",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["vehicle_state"]["window_driver_percent"] == 0


@pytest.mark.asyncio
async def test_alert_polling_keeps_stable_episode_id(client):
    session_id = "api-alerts"
    state = runtime.vehicle.state_for(session_id)
    state.battery_percent = 10
    state.tire_pressures_kpa.front_left = 205
    state.tire_pressures_kpa.rear_right = 315
    state.state_version += 1
    runtime.vehicle._states[session_id] = state

    first = await client.get(f"/api/v1/vehicle/alerts?session_id={session_id}")
    second = await client.get(f"/api/v1/vehicle/alerts?session_id={session_id}")
    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["vehicle_state"]["battery_percent"] == 10
    assert first_payload["vehicle_state"]["tire_pressures_kpa"]["front_left"] == 205
    assert first_payload["vehicle_state"]["tire_pressures_kpa"]["rear_right"] == 315
    assert first_payload["event_sequence"] == second_payload["event_sequence"]
    assert first_payload["active_alerts"][0]["alert_id"] == second_payload["active_alerts"][0]["alert_id"]
    assert first_payload["active_alerts"][0]["code"] == "LOW_BATTERY"


@pytest.mark.asyncio
async def test_stt_deletes_temporary_audio_when_retention_is_disabled(client):
    observed_path: Path | None = None

    async def fake_transcribe(path: Path) -> str:
        nonlocal observed_path
        observed_path = path
        assert path.exists()
        return "Xin chào ViVi"

    with (
        patch.object(runtime.store, "save_audio", return_value=None),
        patch.object(runtime.stt, "transcribe", side_effect=fake_transcribe),
    ):
        response = await client.post(
            "/api/v1/stt",
            data={"session_id": "privacy", "turn_id": "temporary-audio"},
            files={"audio": ("sample.wav", b"RIFF-test", "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["audio_path"] is None
    assert observed_path is not None
    assert not observed_path.exists()


@pytest.mark.asyncio
async def test_durable_turn_status_and_cancel_api(client, monkeypatch, tmp_path):
    from src.vivi.api.routes import turns
    from src.vivi.api.runtime import build_runtime
    from src.vivi.config import Settings

    local = build_runtime(
        Settings(
            data_dir=tmp_path,
            rag_history_db=tmp_path / "history.sqlite3",
            rag_handbook_db=tmp_path / "handbook.sqlite3",
            rag_retrieval_mode="sqlite",
            llm_provider="rules",
            stt_provider="off",
            tts_provider="off",
            vehicle_provider="memory",
            memory_enabled=False,
        )
    )
    monkeypatch.setattr(turns, "runtime", local)
    try:
        created = await client.post(
            "/api/v1/turn", json={"session_id": "driver", "turn_id": "one", "transcript": "đặt nhiệt độ 25 độ"}
        )
        assert created.status_code == 200
        status = await client.get("/api/v1/turns/one", params={"session_id": "driver"})
        assert status.status_code == 200
        assert status.json()["status"] == "completed"
        assert status.json()["command"]["status"] == "verified"
        assert status.json()["response"]["execution"]["executed"] is True
        missing = await client.get("/api/v1/turns/one", params={"session_id": "another"})
        assert missing.status_code == 404
        cancelled = await client.post("/api/v1/turns/one/cancel", json={"session_id": "driver"})
        assert cancelled.json() == {"cancelled": False, "dispatched": False}
    finally:
        local.close()


@pytest.mark.asyncio
async def test_missing_default_model_falls_back_and_reports_actual_provider(client, monkeypatch, tmp_path):
    from src.vivi.api.routes import turns
    from src.vivi.api.runtime import build_runtime
    from src.vivi.config import Settings

    local = build_runtime(
        Settings(
            data_dir=tmp_path,
            rag_history_db=tmp_path / "history.sqlite3",
            rag_handbook_db=tmp_path / "handbook.sqlite3",
            rag_retrieval_mode="sqlite",
            llm_provider="google",
            google_api_key="",
            stt_provider="off",
            tts_provider="off",
            vehicle_provider="memory",
            memory_enabled=False,
        )
    )
    monkeypatch.setattr(turns, "runtime", local)
    try:
        result = await client.post(
            "/api/v1/turn", json={"session_id": "driver", "turn_id": "one", "transcript": "đặt nhiệt độ 30 độ"}
        )
        assert result.status_code == 200
        assert result.json()["provider"] == "langgraph/rules"
        assert result.json()["vehicle_state"]["temperature_celsius"] == 30
        assert local.vehicle.state_for("driver").temperature_celsius == 30
    finally:
        local.close()


@pytest.mark.asyncio
async def test_navigation_snapshot_and_controller_asset_are_available(client):
    snapshot = await client.get("/api/v1/navigation/snapshot", params={"session_id": "api-snapshot"})
    assert snapshot.status_code == 200
    payload = snapshot.json()
    assert payload["runtime_epoch"]
    assert payload["snapshot_sequence"] >= 0
    assert payload["vehicle_state"]["driving"] is False
    assert payload["navigation_workspace"]["version"] == 1
    assert (await client.get("/navigation-state.js")).status_code == 200
