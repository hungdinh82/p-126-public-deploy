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
    try:
        return await runtime.orchestrator.reset_history(request.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
