from __future__ import annotations

import time

from .adapters.llm import LLMAdapter
from .data_store import DataStore
from .safety import validate
from .schemas import ActionProposal, TurnRequest, TurnResponse
from .vehicle import VehicleSimulator
from .vehicle_mqtt import MqttVehicleAdapter


class Orchestrator:
    def __init__(self, llm: LLMAdapter, vehicle: VehicleSimulator | MqttVehicleAdapter, store: DataStore):
        self.llm = llm
        self.vehicle = vehicle
        self.store = store

    async def run(self, request: TurnRequest) -> TurnResponse:
        started = time.perf_counter()
        try:
            vehicle = await self.vehicle.get_state(request.session_id, request.vehicle_state)
        except Exception:
            response = TurnResponse(
                session_id=request.session_id,
                turn_id=request.turn_id,
                transcript=request.transcript,
                provider=self.llm.name,
                status="unverified",
                action=ActionProposal(intent="conversation.clarify"),
                vehicle_state=None,
                message="Chưa kết nối được xe mô phỏng. Mình chưa thực hiện thao tác nào.",
                latency_ms={"total": round((time.perf_counter() - started) * 1000, 2)},
            )
            self.store.append_event(response.model_dump())
            return response
        llm_started = time.perf_counter()
        proposal = await self.llm.propose(request.transcript, vehicle)
        llm_ms = (time.perf_counter() - llm_started) * 1000
        safety = validate(proposal, vehicle)
        if self.vehicle.name == "mqtt" and proposal.intent == "window.set_position":
            message, status = "Lệnh cửa kính cần xác nhận. Luồng xác nhận chưa được bật trong bản demo này.", "blocked"
        elif safety.allowed and proposal.intent == "manual.search":
            message, status = "Mình đã mở cẩm nang minh họa.", "verified"
        elif safety.allowed and proposal.intent == "vehicle.get_status":
            message = f"Xe còn {vehicle.battery_percent} phần trăm pin, nhiệt độ {vehicle.temperature_celsius:g} độ."
            status = "verified"
        elif safety.allowed:
            outcome = await self.vehicle.execute(request.session_id, request.turn_id, proposal, vehicle)
            vehicle, message, status = outcome.state, outcome.message, outcome.status
        else:
            message, status = safety.message, safety.status
        response = TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            transcript=request.transcript,
            provider=self.llm.name,
            status=status,
            action=proposal,
            vehicle_state=vehicle,
            message=message,
            latency_ms={"llm": round(llm_ms, 2), "total": round((time.perf_counter() - started) * 1000, 2)},
        )
        self.store.append_event(response.model_dump())
        return response
