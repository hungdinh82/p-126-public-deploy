from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.vivi.ai_router import JetsonProviderRouter
from src.vivi.api.runtime import runtime
from src.vivi.domain.models import (
    ConfirmationDecisionRequest,
    TurnRequest,
    TurnResponse,
)

router = APIRouter(prefix="/api/v1")
provider_router = JetsonProviderRouter(runtime.settings)


async def _provider(requested: str | None) -> str:
    selected = requested or runtime.orchestrator.default_provider
    if runtime.settings.jetson_ai_policy == "offline_only" and selected not in {"local", "rules"}:
        if requested is not None:
            raise HTTPException(status_code=400, detail="offline_only không cho phép cloud provider")
        selected = "local" if "local" in runtime.orchestrator.graph_providers else "rules"
    if selected not in runtime.orchestrator.graph_providers:
        detail = runtime.orchestrator.provider_errors.get(
            selected, "LLM provider không khả dụng"
        )
        raise HTTPException(status_code=400, detail=detail)
    return await provider_router.select(selected, runtime.orchestrator.graph_providers)


def _reject_inline_confirmation(request: TurnRequest) -> None:
    if request.confirmation_id or request.confirmation_decision:
        raise HTTPException(
            status_code=409,
            detail="Dùng POST /api/v1/confirmations/{id} để xử lý xác nhận.",
        )


@router.post("/turn", response_model=TurnResponse)
async def turn(request: TurnRequest):
    _reject_inline_confirmation(request)
    provider = await _provider(request.llm_provider)
    started = time.perf_counter()
    received_at = time.time()
    try:
        response = await runtime.orchestrator.run(request, provider=provider)
        finished = time.perf_counter()
        runtime.store.update_turn(request.session_id, request.turn_id, {"received_at": received_at, "output_at": time.time(), "end_to_end_ms": round((finished - started) * 1000, 2)})
        return response
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Lượt xử lý đã vượt thời gian chờ.") from exc
    except asyncio.CancelledError as exc:
        raise HTTPException(status_code=409, detail="Lượt xử lý đã được hủy.") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Không xử lý được lượt hội thoại: {exc}"
        ) from exc


@router.post("/turn/stream")
async def turn_stream(request: TurnRequest):
    _reject_inline_confirmation(request)
    provider = await _provider(request.llm_provider)

    async def events():
        started = time.perf_counter()
        received_at = time.time()
        try:
            async for event in runtime.orchestrator.run_stream(request, provider=provider):
                if event.get("type") == "final":
                    finished = time.perf_counter()
                    runtime.store.update_turn(request.session_id, request.turn_id, {"received_at": received_at, "output_at": time.time(), "end_to_end_ms": round((finished - started) * 1000, 2)})
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            yield json.dumps({"type": "cancelled", "turn_id": request.turn_id}, ensure_ascii=False) + "\n"
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


class CancelTurn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)


@router.get("/turns/{turn_id}")
async def turn_status(turn_id: str, session_id: str):
    ledger = runtime.orchestrator.ledger
    value = await asyncio.to_thread(ledger.turn_status, f"{runtime.orchestrator.profile_id}:{session_id}", turn_id, session_id) if ledger else None
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
async def cancel_turn(turn_id: str, request: CancelTurn):
    return runtime.orchestrator.cancel(request.session_id, turn_id)


@router.post("/confirmations/{confirmation_id}", response_model=TurnResponse)
async def decide_confirmation(
    confirmation_id: str, request: ConfirmationDecisionRequest
):
    provider = await _provider(request.llm_provider)
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
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Xác nhận đã vượt thời gian chờ.") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Không xử lý được xác nhận: {exc}"
        ) from exc
