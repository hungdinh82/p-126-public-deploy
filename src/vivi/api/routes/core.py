"""Version 2 conversation endpoints; typed events never probe a model."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.vivi.api.contracts import TurnInput, envelope
from src.vivi.api.routes.roles import require_conversation
from src.vivi.api.routes.turns import _provider
from src.vivi.api.runtime import runtime

router = APIRouter(prefix="/api/v2")


@router.post("/turn")
async def turn(value: TurnInput, request: Request):
    principal = require_conversation(request, value.session_id)
    provider = (
        await _provider(value.provider) if value.input.kind == "utterance" else runtime.orchestrator.default_provider
    )
    try:
        turn_request = value.request().model_copy(update={"profile_id": principal.account_id})
        return envelope(await runtime.orchestrator.run(turn_request, provider=provider))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(504, "Lượt xử lý đã vượt thời gian chờ.") from exc


@router.post("/turn/stream")
async def turn_stream(value: TurnInput, request: Request):
    principal = require_conversation(request, value.session_id)
    provider = (
        await _provider(value.provider) if value.input.kind == "utterance" else runtime.orchestrator.default_provider
    )

    async def events():
        try:
            turn_request = value.request().model_copy(update={"profile_id": principal.account_id})
            async for event in runtime.orchestrator.run_stream(turn_request, provider=provider):
                if event.get("type") == "final":
                    event = {**event, "response": envelope(event["response"])}
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "error_code": "turn_timeout" if isinstance(exc, TimeoutError) else "turn_failed",
                        "turn_id": value.turn_id,
                        "session_id": value.session_id,
                        "detail": "Lượt xử lý đã vượt thời gian chờ." if isinstance(exc, TimeoutError) else str(exc),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
