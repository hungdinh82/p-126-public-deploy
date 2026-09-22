from __future__ import annotations

import time

from .adapters.llm import LLMAdapter
from .data_store import DataStore
from .safety import validate
from .schemas import TurnRequest, TurnResponse
from .vehicle import VehicleSimulator


class Orchestrator:
    def __init__(self, llm: LLMAdapter, vehicle: VehicleSimulator, store: DataStore):
        self.llm = llm
        self.vehicle = vehicle
        self.store = store

    async def run(self, request: TurnRequest) -> TurnResponse:
        started = time.perf_counter()
        vehicle = self.vehicle.state_for(request.session_id, request.vehicle_state)
        llm_started = time.perf_counter()
        proposal = await self.llm.propose(request.transcript, vehicle)
        llm_ms = (time.perf_counter() - llm_started) * 1000
        safety = validate(proposal, vehicle)
        if safety.allowed:
            vehicle, message = await self.vehicle.execute(request.session_id, request.turn_id, proposal)
            status = "verified"
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

