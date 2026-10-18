from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.vivi.api.routes.roles import require_conversation
from src.vivi.api.runtime import runtime

router = APIRouter(prefix="/api/v2")


async def _provider(requested: str | None) -> str:
    selected = requested or runtime.orchestrator.default_provider
    if selected == "rules":
        raise HTTPException(status_code=400, detail="rules không phải model provider. Hãy chọn cloud hoặc local.")
    if (runtime.settings.jetson_ai_policy == "offline_only" or runtime.settings.rag_local_only) and selected != "local":
        if requested is not None:
            raise HTTPException(status_code=400, detail="Chế độ offline không cho phép cloud provider")
        return "local"
    if selected not in {"local", "google", "openai", "openrouter"}:
        raise HTTPException(status_code=400, detail="Model provider không hợp lệ")
    return selected


class CancelTurn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)


@router.get("/turns/{turn_id}")
async def turn_status(turn_id: str, session_id: str, request: Request):
    principal = require_conversation(request, session_id)
    ledger = runtime.orchestrator.ledger
    value = (
        await asyncio.to_thread(
            ledger.turn_status, f"{principal.account_id}:{session_id}", turn_id, session_id
        )
        if ledger
        else None
    )
    if value is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy lượt xử lý.")
    # Observations aid reconciliation but never prove attribution or trigger replay.
    if value["command"] and value["command"]["status"] in {"dispatched", "unknown"}:
        try:
            observed = await runtime.vehicle.get_state(session_id)
            value["observed_vehicle_state"] = observed.model_dump(mode="json")
        except Exception:
            value["observed_vehicle_state"] = None
    return value


@router.post("/turns/{turn_id}/cancel")
async def cancel_turn(turn_id: str, request: CancelTurn, http_request: Request):
    require_conversation(http_request, request.session_id)
    return runtime.orchestrator.cancel(request.session_id, turn_id)
