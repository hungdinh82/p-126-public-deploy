from __future__ import annotations

import pytest

from src.vivi.api.routes.engineer import service
from src.vivi.api.runtime import runtime
from src.vivi.engineer.service import SimulatorControlAdapter


async def engineer_client(client):
    selected = await client.post("/api/v1/session/role", json={"role": "engineer"})
    assert selected.status_code == 200
    return client


@pytest.mark.asyncio
async def test_role_selection_and_protected_pages(client):
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/engineer", follow_redirects=False)).status_code == 303
    assert (await client.get("/api/v1/engineer/fleet")).status_code == 403
    await engineer_client(client)
    assert (await client.get("/engineer")).status_code == 200
    assert (await client.get("/api/v1/engineer/overview")).status_code == 200


@pytest.mark.asyncio
async def test_driver_role_can_open_ivi_and_engineer_is_rejected(client):
    selected = await client.post("/api/v1/session/role", json={"role": "driver"})
    assert selected.json()["redirect"] == "/ivi"
    assert (await client.get("/ivi")).status_code == 200
    assert (await client.get("/api/v1/engineer/fleet")).status_code == 403


@pytest.mark.asyncio
async def test_fleet_labels_memory_and_digital_twins(client):
    await engineer_client(client)
    response = await client.get("/api/v1/engineer/fleet")
    payload = response.json()
    primary = next(item for item in payload["vehicles"] if item["vehicle_id"] == runtime.settings.vehicle_id)
    assert primary["source"] == "memory runtime"
    assert primary["availability"] == "memory"
    twins = [item for item in payload["vehicles"] if item["vehicle_id"] != runtime.settings.vehicle_id]
    assert {item["vehicle_id"] for item in twins} == {"demo-car-2", "demo-car-3"}
    assert all(item["source"] == "simulated twin" and item["availability"] == "simulated" for item in twins)


@pytest.mark.asyncio
async def test_scenarios_change_only_target_and_alerts_recover(client):
    await engineer_client(client)
    before = service.twins["demo-car-3"].state.model_copy(deep=True)
    applied = await client.post("/api/v1/engineer/vehicles/demo-car-2/scenario", json={"scenario": "low_battery"})
    assert applied.status_code == 200
    assert applied.json()["mode"] == "simulation"
    fleet = (await client.get("/api/v1/engineer/fleet")).json()["vehicles"]
    twin = next(item for item in fleet if item["vehicle_id"] == "demo-car-2")
    assert twin["state"]["battery_percent"] == 15
    assert any(alert["code"] == "LOW_BATTERY" for alert in twin["alerts"])
    assert service.twins["demo-car-3"].state.battery_percent == before.battery_percent
    reset = await client.post("/api/v1/engineer/vehicles/demo-car-2/scenario", json={"scenario": "reset"})
    assert reset.status_code == 200
    assert not service.twin_alerts["demo-car-2"].evaluate(service.twins["demo-car-2"].state).active_alerts


@pytest.mark.asyncio
async def test_runtime_memory_scenario_uses_runtime_vehicle_and_audits(client):
    await engineer_client(client)
    response = await client.post("/api/v1/engineer/vehicles/demo-car-1/scenario", json={"scenario": "front_left_low"})
    assert response.status_code == 200
    assert response.json()["mode"] == "memory"
    assert runtime.vehicle.state_for("engineer-dashboard").tire_pressures_kpa.front_left == 205
    audit = (await client.get("/api/v1/engineer/audit")).json()["events"]
    assert audit[0]["action"] == "scenario.apply"


@pytest.mark.asyncio
async def test_mqtt_scenario_uses_simulator_control_adapter(client, monkeypatch):
    await engineer_client(client)
    calls = []

    async def controlled_apply(_self, vehicle_id, scenario):
        calls.append((vehicle_id, scenario))

    monkeypatch.setattr(runtime.settings, "vehicle_provider", "mqtt")
    monkeypatch.setattr(SimulatorControlAdapter, "apply", controlled_apply)
    response = await client.post("/api/v1/engineer/vehicles/demo-car-1/scenario", json={"scenario": "reset"})
    assert response.status_code == 200
    assert response.json()["mode"] == "live"
    assert calls == [("demo-car-1", "reset")]


@pytest.mark.asyncio
async def test_disconnected_runtime_is_reported_unavailable(client, monkeypatch):
    await engineer_client(client)
    monkeypatch.setattr(runtime.vehicle, "is_connected", lambda: False)
    fleet = (await client.get("/api/v1/engineer/fleet")).json()["vehicles"]
    primary = next(item for item in fleet if item["vehicle_id"] == "demo-car-1")
    assert primary["online"] is False
    assert primary["availability"] == "unavailable"


@pytest.mark.asyncio
async def test_model_and_ota_are_simulations_and_never_change_settings(client):
    await engineer_client(client)
    old_provider = runtime.settings.llm_provider
    model = await client.post("/api/v1/engineer/models/apply", json={"profile_id": "pc_offline"})
    assert model.json() == {"selected_profile": "pc_offline", "mode": "simulation", "changed_runtime": False}
    assert runtime.settings.llm_provider == old_provider
    rollback = await client.post("/api/v1/engineer/ota/deploy", json={"outcome": "rollback"})
    assert rollback.json()["result"] == "ROLLED_BACK"
    assert rollback.json()["mode"] == "simulation"


@pytest.mark.asyncio
async def test_engineer_contract_contains_no_mqtt_secret(client):
    await engineer_client(client)
    payload = (await client.get("/api/v1/engineer/overview")).text.lower()
    assert "mqtt_api_password" not in payload
    assert runtime.settings.mqtt_api_password.lower() not in payload if runtime.settings.mqtt_api_password else True
