from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from src.vivi.api.routes.roles import require_role
from src.vivi.api.runtime import runtime
from src.vivi.engineer.service import (
    EngineerError,
    EngineerService,
    ModelSelectionRequest,
    OtaDeployRequest,
    ScenarioRequest,
)

router = APIRouter(prefix="/api/v1/engineer")
service = EngineerService(runtime)


def engineer_role(request: Request) -> str:
    return require_role("engineer")(request)


def _http_error(error: EngineerError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


@router.get("/overview")
async def overview(_: str = Depends(engineer_role)):
    return await service.snapshot()


@router.get("/fleet")
async def fleet(_: str = Depends(engineer_role)):
    return await service.fleet()


@router.get("/scenarios")
async def scenarios(_: str = Depends(engineer_role)):
    return await service.scenarios()


@router.post("/vehicles/{vehicle_id}/scenario")
async def apply_scenario(vehicle_id: str, request: ScenarioRequest, actor: str = Depends(engineer_role)):
    try:
        return await service.apply_scenario(vehicle_id, request, actor)
    except EngineerError as exc:
        raise _http_error(exc) from exc


@router.get("/models")
async def models(_: str = Depends(engineer_role)):
    return await service.models()


@router.post("/models/apply")
async def apply_model(request: ModelSelectionRequest, actor: str = Depends(engineer_role)):
    return await service.select_model_profile(request, actor)


@router.get("/ota")
async def ota(_: str = Depends(engineer_role)):
    return await service.ota()


@router.post("/ota/deploy")
async def deploy_ota(request: OtaDeployRequest, actor: str = Depends(engineer_role)):
    return await service.deploy_ota(request, actor)


@router.get("/audit")
async def audit(_: str = Depends(engineer_role)):
    return await service.audit_events()
