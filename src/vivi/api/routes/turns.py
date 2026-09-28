from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.vivi.api.runtime import runtime
from src.vivi.domain.models import (
    ConfirmationDecisionRequest,
    TurnRequest,
    TurnResponse,
)

router = APIRouter(prefix="/api/v1")


def _provider(requested: str | None) -> str:
    selected = requested or runtime.orchestrator.default_provider
    if selected not in runtime.orchestrator.graph_providers:
        detail = runtime.orchestrator.provider_errors.get(
            selected, "LLM provider không khả dụng"
        )
        raise HTTPException(status_code=400, detail=detail)
    return selected


def _reject_inline_confirmation(request: TurnRequest) -> None:
    if request.confirmation_id or request.confirmation_decision:
        raise HTTPException(
            status_code=409,
            detail="Dùng POST /api/v1/confirmations/{id} để xử lý xác nhận.",
        )


@router.post("/turn", response_model=TurnResponse)
async def turn(request: TurnRequest):
    _reject_inline_confirmation(request)
    provider = _provider(request.llm_provider)
    try:
        return await runtime.orchestrator.run(request, provider=provider)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Không xử lý được lượt hội thoại: {exc}"
        ) from exc


@router.post("/turn/stream")
async def turn_stream(request: TurnRequest):
    _reject_inline_confirmation(request)
    provider = _provider(request.llm_provider)

    async def events():
        try:
            async for event in runtime.orchestrator.run_stream(request, provider=provider):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps(
                {"type": "error", "detail": f"Không xử lý được lượt hội thoại: {exc}"},
                ensure_ascii=False,
            ) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/confirmations/{confirmation_id}", response_model=TurnResponse)
async def decide_confirmation(
    confirmation_id: str, request: ConfirmationDecisionRequest
):
    provider = _provider(request.llm_provider)
    turn_request = TurnRequest(
        transcript="Xác nhận" if request.decision == "approve" else "Hủy",
        session_id=request.session_id,
        turn_id=request.turn_id,
        confirmation_id=confirmation_id,
        confirmation_decision=request.decision,
        llm_provider=request.llm_provider,
    )
    try:
        return await runtime.orchestrator.run(turn_request, provider=provider)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Không xử lý được xác nhận: {exc}"
        ) from exc
