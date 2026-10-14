import asyncio

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from src.vivi.api.runtime import runtime

router = APIRouter(prefix="/api/v1/history", tags=["history"])


class HistoryReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(default=None, min_length=1, max_length=100)


@router.post("/reset")
async def reset_history(request: HistoryReset, response: Response):
    response.headers["Cache-Control"] = "no-store"
    voice = getattr(runtime, "voice", None)
    if voice is not None:
        await voice.close()
    try:
        result = await runtime.orchestrator.reset_history(request.session_id)
        if voice is not None:
            await asyncio.to_thread(voice.inbox.clear, request.session_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if voice is not None:
            await voice.start()
