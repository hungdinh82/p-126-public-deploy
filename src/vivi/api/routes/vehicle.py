from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.vivi.api.runtime import runtime
from src.vivi.domain.models import DemoDrivingRequest, VehicleState
from src.vivi.vehicle.alerts import AlertSnapshot
from src.vivi.vehicle.memory import VehicleSimulator

router = APIRouter(prefix="/api/v1")


class VehicleAlertsResponse(AlertSnapshot):
    vehicle_state: VehicleState


@router.get("/vehicle/state")
async def vehicle_state(session_id: str = Query(default="api-read", min_length=1, max_length=100)):
    try:
        return await runtime.vehicle.get_state(session_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Xe mô phỏng chưa sẵn sàng") from exc


@router.get("/vehicle/alerts", response_model=VehicleAlertsResponse)
async def vehicle_alerts(session_id: str = Query(default="api-read", min_length=1, max_length=100)):
    try:
        state = await runtime.vehicle.get_state(session_id)
        snapshot = runtime.alerts.evaluate(state)
        return VehicleAlertsResponse(**snapshot.model_dump(), vehicle_state=state)
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
