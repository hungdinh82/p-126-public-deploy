from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_health_uses_canonical_edge_app(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["orchestration"]["engine"] == "langgraph"
    assert payload["orchestration"]["retrieval"] == "sqlite_fts5"


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
async def test_stt_deletes_temporary_audio_when_retention_is_disabled(client):
    observed_path: Path | None = None

    async def fake_transcribe(path: Path) -> str:
        nonlocal observed_path
        observed_path = path
        assert path.exists()
        return "Xin chào ViVi"

    with patch("server.app.store.save_audio", return_value=None), patch(
        "server.app.stt.transcribe", side_effect=fake_transcribe
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
