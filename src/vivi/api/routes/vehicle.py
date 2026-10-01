from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.vivi.api.runtime import runtime
from src.vivi.domain.models import DemoDrivingRequest, VehicleState
from src.vivi.vehicle.memory import VehicleSimulator

router = APIRouter(prefix="/api/v1")


@router.get("/vehicle/state")
async def vehicle_state():
    try:
        return await runtime.vehicle.get_state("api-read")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Xe mô phỏng chưa sẵn sàng") from exc


@router.put("/demo/vehicle/driving", response_model=VehicleState)
async def set_demo_driving(request: DemoDrivingRequest):
    if not isinstance(runtime.vehicle, VehicleSimulator):
        raise HTTPException(
            status_code=409,
            detail="Chế độ lái demo chỉ khả dụng với VIVI_VEHICLE_PROVIDER=memory.",
        )
    try:
        return runtime.vehicle.set_driving(request.session_id, request.driving)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
