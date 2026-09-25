import pytest

from src.api import routes


@pytest.mark.asyncio
async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_chat_empty_message(client):
    response = await client.post("/api/v1/chat", json={"message": ""})
    assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_agent_status(client):
    response = await client.get("/api/v1/status")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_assist_accepts_stt_text_and_returns_output_envelope(client, monkeypatch):
    class FakeGraph:
        async def ainvoke(self, state):
            return {
                "output": {
                    "session_id": state["session_id"],
                    "turn_id": state["turn_id"],
                    "route": "action",
                    "intent": "climate.set_temperature",
                    "status": "action_verified",
                    "response_text": "Mình đã đặt nhiệt độ ở 25 độ.",
                    "tts_text": "Mình đã đặt nhiệt độ ở 25 độ.",
                    "action_proposal": {
                        "intent": "climate.set_temperature",
                        "arguments": {"value_celsius": 25},
                        "confidence": 1,
                    },
                    "requires_execution": False,
                    "execution": {
                        "allowed": True,
                        "executed": True,
                        "verified": True,
                        "risk_class": "R1",
                        "message": "Mình đã đặt nhiệt độ ở 25 độ.",
                    },
                    "vehicle_state": {"temperature_celsius": 25},
                }
            }

    monkeypatch.setattr(routes, "runtime_agent", lambda: FakeGraph())
    response = await client.post(
        "/api/v1/assist",
        json={"input_text": "Đặt nhiệt độ 25 độ", "session_id": "api", "turn_id": "t1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["route"] == "action"
    assert payload["requires_execution"] is False
    assert payload["execution"]["verified"] is True
    assert payload["action_proposal"]["arguments"] == {"value_celsius": 25}


@pytest.mark.asyncio
async def test_assist_rejects_invalid_vehicle_context_before_running_graph(client):
    response = await client.post(
        "/api/v1/assist",
        json={
            "input_text": "Mở cửa sổ",
            "session_id": "api",
            "turn_id": "bad-state",
            "vehicle_state": {"window_driver_percent": 120},
        },
    )
    assert response.status_code == 422
