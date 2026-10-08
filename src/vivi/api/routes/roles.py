from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1")
_sessions: dict[str, str] = {}
COOKIE_NAME = "vivi_demo_session"


class RoleSelection(BaseModel):
    role: str


def role_for(request: Request) -> str | None:
    return _sessions.get(request.cookies.get(COOKIE_NAME, ""))


def require_role(role: str) -> Callable:
    def check(request: Request) -> str:
        actual = role_for(request)
        if actual != role:
            raise HTTPException(status_code=403, detail="Vai trò demo không hợp lệ hoặc phiên đã hết hạn.")
        return actual
    return check


@router.post("/session/role")
async def select_role(selection: RoleSelection, response: Response):
    if selection.role not in {"driver", "engineer"}:
        raise HTTPException(status_code=422, detail="Vai trò không hợp lệ.")
    token = secrets.token_urlsafe(24)
    _sessions[token] = selection.role
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", secure=False, max_age=60 * 60 * 8)
    return {"role": selection.role, "redirect": "/ivi" if selection.role == "driver" else "/engineer"}


@router.delete("/session")
async def clear_role(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _sessions.pop(token, None)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
