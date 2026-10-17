from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel

from src.vivi.domain.models import VehicleState
from src.vivi.vehicle.memory import VehicleSimulator

ENGINEER_SESSION = "engineer-dashboard"
SIMULATED_IDS = ("demo-car-2", "demo-car-3")


class EngineerError(Exception):
    def __init__(self, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class ScenarioRequest(BaseModel):
    scenario: Literal[
        "reset",
        "low_battery",
        "front_left_low",
        "rear_right_high",
        "powertrain_hot",
        "driver_door_open",
        "disconnect",
    ]


class ModelSelectionRequest(BaseModel):
    profile_id: Literal[
        "auto",
        "cloud_only",
        "jetson_only",
        "fallback_only",
        "runtime",
        "pc_offline",
        "jetson_offline",
    ]


class ConnectivityScenarioRequest(BaseModel):
    scenario: Literal[
        "actual",
        "internet_down",
        "jetson_down",
        "both_down",
        "stt_down",
        "llm_down",
        "tts_down",
    ]


class OtaDeployRequest(BaseModel):
    outcome: Literal["success", "rollback"] = "success"


class AuditEvent(BaseModel):
    occurred_at: datetime
    actor: str
    action: str
    target: str
    detail: str
    result: str = "ok"


class InMemoryTwin:
    """A deliberately labelled Digital Twin; it never represents a live vehicle."""

    def __init__(self, vehicle_id: str) -> None:
        self.vehicle_id = vehicle_id
        self.state = VehicleState(vehicle_id=vehicle_id)
        self.connected = True

    def apply(self, scenario: str) -> VehicleState:
        if scenario == "reset":
            self.state = VehicleState(
                vehicle_id=self.vehicle_id,
                state_version=self.state.state_version + 1,
            )
            self.connected = True
            return self.state
        if scenario == "disconnect":
            self.connected = False
            return self.state
        updates = _scenario_updates(scenario)
        data = self.state.model_dump()
        data.update(updates)
        data["state_version"] = self.state.state_version + 1
        data["updated_at"] = datetime.now(UTC)
        self.state = VehicleState.model_validate(data)
        self.connected = True
        return self.state


def _scenario_updates(scenario: str) -> dict[str, Any]:
    if scenario == "low_battery":
        return {"battery_percent": 15}
    if scenario == "front_left_low":
        return {"tire_pressures_kpa": {"front_left": 205, "front_right": 250, "rear_left": 250, "rear_right": 250}}
    if scenario == "rear_right_high":
        return {"tire_pressures_kpa": {"front_left": 250, "front_right": 250, "rear_left": 250, "rear_right": 315}}
    if scenario == "powertrain_hot":
        return {"powertrain_temperature_celsius": 95}
    if scenario == "driver_door_open":
        return {"power_state": "ready", "door_states": {"driver": {"open": True, "locked": False}}}
    raise EngineerError("Scenario không được hỗ trợ.", 422)


class SimulatorControlAdapter:
    """Narrow HTTP seam for the local simulator's explicitly enabled test controls."""

    def __init__(self, base_url: str, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def apply(self, vehicle_id: str, scenario: str) -> None:
        path = f"/api/v1/test/vehicles/{vehicle_id}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                if scenario == "reset":
                    response = await client.post(self.base_url + path + "/reset")
                else:
                    response = await client.put(self.base_url + path + "/fixture", json=_scenario_updates(scenario))
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {404, 503}:
                    raise EngineerError(
                        "Vehicle Simulator chưa sẵn sàng hoặc chưa bật --enable-test-control.", 503
                    ) from exc
                raise EngineerError("Vehicle Simulator từ chối scenario.", 502) from exc
            except httpx.HTTPError as exc:
                raise EngineerError("Không kết nối được Vehicle Simulator test-control.", 503) from exc


class EngineerService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.twins = {vehicle_id: InMemoryTwin(vehicle_id) for vehicle_id in SIMULATED_IDS}
        # The memory runtime is session-scoped while AlertEngine is vehicle-scoped.
        # Keep this dashboard observer separate so it cannot contaminate the driver's
        # alert episode for the same demo-car-1 identifier.
        from src.vivi.vehicle.alerts import AlertEngine

        self.dashboard_alerts = AlertEngine()
        self.twin_alerts = {vehicle_id: AlertEngine() for vehicle_id in SIMULATED_IDS}
        self.audit: deque[AuditEvent] = deque(maxlen=100)
        self.ota_history: deque[dict[str, Any]] = deque(maxlen=20)
        self.selected_profile = "runtime"

    def _record(self, actor: str, action: str, target: str, detail: str, result: str = "ok") -> None:
        self.audit.appendleft(
            AuditEvent(
                occurred_at=datetime.now(UTC), actor=actor, action=action, target=target, detail=detail, result=result
            )
        )

    async def snapshot(self) -> dict[str, Any]:
        # These are the canonical endpoint functions, so hardware and pipeline
        # collection remains owned by the existing health/metrics routes.
        from src.vivi.api.routes.health import health
        from src.vivi.api.routes.metrics import metrics

        health_payload, metrics_payload = await health(), metrics()
        return {
            "health": health_payload,
            "metrics": metrics_payload,
            "selected_profile": self.runtime.connectivity.profile,
            "connectivity": self.runtime.connectivity.snapshot(),
        }

    def ai_routes(self) -> dict[str, Any]:
        return {"routes": self.runtime.store.ai_route_snapshot()}

    async def _runtime_state(self) -> VehicleState | None:
        try:
            return await self.runtime.vehicle.get_state(ENGINEER_SESSION)
        except Exception:
            return None

    def _vehicle_payload(
        self, state: VehicleState | None, *, source: str, connected: bool, model_profile: str
    ) -> dict[str, Any]:
        if state is None:
            return {
                "vehicle_id": self.runtime.settings.vehicle_id,
                "source": source,
                "availability": "unavailable",
                "online": False,
                "last_seen": None,
                "stale_seconds": None,
                "state": None,
                "alerts": [],
                "software_profile": model_profile,
            }
        alerts = (
            self.dashboard_alerts
            if state.vehicle_id == self.runtime.settings.vehicle_id
            else self.twin_alerts[state.vehicle_id]
        ).evaluate(state)
        now = datetime.now(UTC)
        stale = max(0.0, (now - state.updated_at).total_seconds())
        availability = (
            "live" if source == "mqtt live" and connected else "memory" if source == "memory runtime" else "simulated"
        )
        if not connected:
            availability = "unavailable"
        return {
            "vehicle_id": state.vehicle_id,
            "source": source,
            "availability": availability,
            "online": connected,
            "last_seen": state.updated_at,
            "stale_seconds": round(stale, 1),
            "state": state,
            "alerts": alerts.active_alerts,
            "software_profile": model_profile,
        }

    async def fleet(self) -> dict[str, Any]:
        state = await self._runtime_state()
        provider = self.runtime.settings.vehicle_provider
        runtime_source = "mqtt live" if provider == "mqtt" else "memory runtime"
        vehicles = [
            self._vehicle_payload(
                state, source=runtime_source, connected=self.runtime.vehicle.is_connected(), model_profile="runtime"
            )
        ]
        for twin in self.twins.values():
            vehicles.append(
                self._vehicle_payload(
                    twin.state, source="simulated twin", connected=twin.connected, model_profile="simulation"
                )
            )
        return {"vehicles": vehicles}

    async def scenarios(self) -> dict[str, Any]:
        return {
            "scenarios": [
                {"id": "reset", "label": "Reset mặc định"},
                {"id": "low_battery", "label": "Pin thấp 15%"},
                {"id": "front_left_low", "label": "Lốp trước trái thấp 205 kPa"},
                {"id": "rear_right_high", "label": "Lốp sau phải cao 315 kPa"},
                {"id": "powertrain_hot", "label": "Hệ truyền động 95°C"},
                {"id": "driver_door_open", "label": "Cửa tài mở khi READY"},
                {"id": "disconnect", "label": "Mất kết nối twin", "simulated_only": True},
            ]
        }

    async def apply_scenario(self, vehicle_id: str, request: ScenarioRequest, actor: str) -> dict[str, Any]:
        scenario = request.scenario
        if vehicle_id in self.twins:
            state = self.twins[vehicle_id].apply(scenario)
            self.twin_alerts[vehicle_id].evaluate(state)
            self._record(actor, "scenario.apply", vehicle_id, f"{scenario}; mode: simulation")
            return {"mode": "simulation", "vehicle_id": vehicle_id, "scenario": scenario, "state": state}
        if vehicle_id != self.runtime.settings.vehicle_id:
            raise EngineerError("Không tìm thấy vehicle_id.", 404)
        if scenario == "disconnect":
            raise EngineerError("Mất kết nối chỉ áp dụng cho Digital Twin mô phỏng.", 422)
        if self.runtime.settings.vehicle_provider == "mqtt":
            await SimulatorControlAdapter(self.runtime.settings.vehicle_simulator_url).apply(vehicle_id, scenario)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = await self._runtime_state()
                if state is not None:
                    self.dashboard_alerts.evaluate(state)
                    self._record(actor, "scenario.apply", vehicle_id, f"{scenario}; mode: mqtt test-control")
                    return {"mode": "live", "vehicle_id": vehicle_id, "scenario": scenario, "state": state}
                await asyncio.sleep(0.1)
            raise EngineerError("Simulator đã nhận lệnh nhưng runtime chưa nhận state MQTT mới.", 503)
        if not isinstance(self.runtime.vehicle, VehicleSimulator):
            raise EngineerError("Runtime vehicle không hỗ trợ scenario này.", 409)
        previous = self.runtime.vehicle.state_for(ENGINEER_SESSION)
        if scenario == "reset":
            state = VehicleState(vehicle_id=vehicle_id, state_version=previous.state_version + 1)
        else:
            data = previous.model_dump()
            data.update(_scenario_updates(scenario))
            data["state_version"] = previous.state_version + 1
            data["updated_at"] = datetime.now(UTC)
            state = VehicleState.model_validate(data)
        state = self.runtime.vehicle.apply_shared_fixture(state)
        self.dashboard_alerts.evaluate(state)
        self._record(actor, "scenario.apply", vehicle_id, f"{scenario}; mode: memory runtime")
        return {"mode": "memory", "vehicle_id": vehicle_id, "scenario": scenario, "state": state}

    async def models(self) -> dict[str, Any]:
        settings = self.runtime.settings
        return {
            "selected_profile": self.runtime.connectivity.profile,
            "profiles": [
                {
                    "id": "auto",
                    "label": "Tự động",
                    "mode": "runtime",
                    "verified": True,
                    "detail": f"Cloud khi có Internet; Jetson khi offline; cuối cùng fallback giới hạn ({settings.llm_provider})",
                },
                {
                    "id": "cloud_only",
                    "label": "Chỉ Cloud",
                    "mode": "runtime",
                    "verified": True,
                    "detail": "Không gọi Jetson; vẫn giữ fallback giới hạn, RAG và điều khiển xe trên Laptop",
                },
                {
                    "id": "jetson_only",
                    "label": "Chỉ Jetson",
                    "mode": "runtime",
                    "verified": True,
                    "detail": "Không gọi Cloud; vẫn giữ fallback giới hạn, RAG và điều khiển xe trên Laptop",
                },
                {
                    "id": "fallback_only",
                    "label": "Fallback giới hạn",
                    "mode": "runtime",
                    "verified": True,
                    "detail": "Không gọi LLM; dùng nút thao tác, xác nhận đang chờ và RAG trích xuất",
                },
            ],
        }

    async def select_model_profile(self, request: ModelSelectionRequest, actor: str) -> dict[str, Any]:
        if request.profile_id in {"runtime", "pc_offline", "jetson_offline"}:
            # Compatibility for older dashboard clients. New clients use the
            # four explicit runtime profiles above.
            self.selected_profile = request.profile_id
            self._record(actor, "model.select", request.profile_id, "legacy simulation; config không thay đổi")
            return {"selected_profile": request.profile_id, "mode": "simulation", "changed_runtime": False}
        self.selected_profile = request.profile_id
        self.runtime.connectivity.set_profile(request.profile_id)
        self._record(actor, "model.select", request.profile_id, "runtime routing profile changed")
        return {"selected_profile": request.profile_id, "mode": "runtime", "changed_runtime": True}

    async def connectivity_scenarios(self) -> dict[str, Any]:
        return {
            "selected": self.runtime.connectivity.override,
            "scenarios": [
                {"id": "actual", "label": "Trạng thái thực / Auto"},
                {"id": "internet_down", "label": "Mất Internet"},
                {"id": "jetson_down", "label": "Mất Jetson"},
                {"id": "both_down", "label": "Mất cả Internet và Jetson"},
                {"id": "stt_down", "label": "Chỉ STT Jetson lỗi"},
                {"id": "llm_down", "label": "Chỉ LLM Jetson lỗi"},
                {"id": "tts_down", "label": "Chỉ TTS Jetson lỗi"},
            ],
            "state": self.runtime.connectivity.snapshot(),
        }

    async def apply_connectivity_scenario(self, request: ConnectivityScenarioRequest, actor: str) -> dict[str, Any]:
        self.runtime.connectivity.set_override(request.scenario)
        self._record(actor, "connectivity.apply", "runtime", request.scenario)
        return self.runtime.connectivity.snapshot()

    async def ota(self) -> dict[str, Any]:
        return {"simulation": True, "history": list(self.ota_history)}

    async def deploy_ota(self, request: OtaDeployRequest, actor: str) -> dict[str, Any]:
        states = ["READY", "CANARY_DEPLOYING", "CANARY_HEALTHY", "ROLLING_OUT", "COMPLETED"]
        if request.outcome == "rollback":
            states = ["READY", "CANARY_DEPLOYING", "HEALTH_CHECK_FAILED", "ROLLING_BACK", "ROLLED_BACK"]
        record = {
            "id": str(uuid4()),
            "occurred_at": datetime.now(UTC),
            "states": states,
            "result": states[-1],
            "mode": "simulation",
        }
        self.ota_history.appendleft(record)
        self._record(actor, "ota.deploy", record["id"][:8], f"{' → '.join(states)}; mode: simulation")
        return record

    async def audit_events(self) -> dict[str, Any]:
        return {"events": list(self.audit)}
