from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from src.vivi.api.routes.roles import require_conversation, require_principal
from src.vivi.api.runtime import runtime

router = APIRouter(prefix="/api/v1/history", tags=["history"])


class HistoryReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(default=None, min_length=1, max_length=100)


@router.post("/reset")
async def reset_history(request: HistoryReset, response: Response, http_request: Request):
    response.headers["Cache-Control"] = "no-store"
    principal = None
    if request.session_id:
        principal = require_conversation(http_request, request.session_id)
    elif runtime.settings.app_env != "test" and runtime.settings.auth_required:
        require_principal(http_request)
        raise HTTPException(status_code=422, detail="Cần chỉ định cuộc trò chuyện cần xoá.")
    try:
        return await runtime.orchestrator.reset_history(request.session_id, principal.account_id if principal else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
