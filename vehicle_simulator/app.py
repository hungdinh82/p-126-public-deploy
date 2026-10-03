from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi import Path as ApiPath

from .engine import VehicleSimulator
from .models import CommandResult, FaultScenario, VehicleCommand, VehicleFixture, VehicleState


def create_app(
    database_path: Path,
    *,
    enable_test_control: bool = False,
    simulator: VehicleSimulator | None = None,
    publish_state: Callable[[VehicleState], object] | None = None,
) -> FastAPI:
    owns_simulator = simulator is None
    simulator = simulator or VehicleSimulator(database_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_simulator:
            simulator.close()

    app = FastAPI(title="ViVi Vehicle Simulator", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "vehicle-simulator", "transport": "http"}

    @app.get("/api/v1/vehicles/{vehicle_id}/state", response_model=VehicleState)
    def get_state(vehicle_id: str = ApiPath(min_length=1, max_length=100)):
        return simulator.get_state(vehicle_id)

    @app.post("/api/v1/vehicles/{vehicle_id}/commands", response_model=CommandResult)
    async def execute(command: VehicleCommand, vehicle_id: str = ApiPath(min_length=1, max_length=100)):
        if vehicle_id != command.vehicle_id:
            raise HTTPException(status_code=400, detail="vehicle_mismatch")
        fault = simulator.fault_for(vehicle_id)
        if fault.mode == "disconnected":
            raise HTTPException(status_code=503, detail="simulated_disconnected")
        if fault.mode == "timeout_before_apply":
            raise HTTPException(status_code=504, detail="simulated_timeout_before_apply")
        if fault.mode == "delay" and fault.delay_ms:
            await asyncio.sleep(fault.delay_ms / 1000)
        result = await asyncio.to_thread(simulator.execute, command, fault)
        if fault.mode == "ack_lost_after_apply" and result.ack.status == "applied":
            raise HTTPException(status_code=504, detail="simulated_ack_lost_after_apply")
        return result

    if enable_test_control:

        @app.put("/api/v1/test/vehicles/{vehicle_id}/fault", response_model=FaultScenario)
        def set_fault(fault: FaultScenario, vehicle_id: str = ApiPath(min_length=1, max_length=100)):
            return simulator.set_fault(vehicle_id, fault)

        @app.put("/api/v1/test/vehicles/{vehicle_id}/fixture", response_model=VehicleState)
        def set_fixture(fixture: VehicleFixture, vehicle_id: str = ApiPath(min_length=1, max_length=100)):
            try:
                state = simulator.set_fixture(vehicle_id, fixture)
                if publish_state:
                    publish_state(state)
                return state
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/api/v1/test/vehicles/{vehicle_id}/reset", response_model=VehicleState)
        def reset(vehicle_id: str = ApiPath(min_length=1, max_length=100)):
            state = simulator.reset(vehicle_id)
            if publish_state:
                publish_state(state)
            return state

    return app
