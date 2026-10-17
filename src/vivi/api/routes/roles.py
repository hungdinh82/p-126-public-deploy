from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from src.vivi.api.runtime import runtime
from src.vivi.identity import Principal

router = APIRouter(prefix="/api/v1")
COOKIE_NAME = "vivi_demo_session"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=8, max_length=30)
    pin: str = Field(min_length=4, max_length=12)


class RoleSelection(BaseModel):
    role: str


def principal_for(request: Request) -> Principal | None:
    sessions = getattr(runtime, "sessions", None)
    return sessions.principal(request.cookies.get(COOKIE_NAME)) if sessions else None


def role_for(request: Request) -> str | None:
    principal = principal_for(request)
    return principal.role if principal else None


def require_principal(request: Request) -> Principal:
    principal = principal_for(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Vui lòng đăng nhập để tiếp tục.")
    return principal


def require_conversation(request: Request, conversation_id: str) -> Principal:
    # Existing graph tests intentionally run without browser authentication.
    if runtime.settings.app_env == "test" or not runtime.settings.auth_required:
        return Principal(account_id="legacy", phone="", role="driver")
    principal = require_principal(request)
    if not runtime.sessions.owns_conversation(principal.account_id, conversation_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy cuộc trò chuyện.")
    return principal


def require_role(role: str) -> Callable:
    def check(request: Request) -> str:
        principal = principal_for(request)
        if principal is None or principal.role != role:
            raise HTTPException(status_code=403, detail="Vai trò không hợp lệ hoặc phiên đã hết hạn.")
        return principal.role

    return check


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=runtime.settings.app_env == "production",
        max_age=runtime.settings.auth_session_hours * 60 * 60,
    )


@router.post("/session/login")
async def login(value: LoginRequest, response: Response):
    try:
        token, principal = runtime.sessions.login(value.phone, value.pin)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_cookie(response, token)
    conversation_id = runtime.sessions.create_conversation(principal.account_id) if principal.role == "driver" else None
    return {
        "role": principal.role,
        "conversation_id": conversation_id,
        "redirect": "/ivi" if principal.role == "driver" else "/engineer",
    }


@router.get("/session")
async def current_session(request: Request):
    principal = require_principal(request)
    return {"phone": principal.phone, "role": principal.role}


@router.post("/session/conversations", status_code=201)
async def create_conversation(request: Request):
    principal = require_principal(request)
    if principal.role != "driver":
        raise HTTPException(status_code=403, detail="Chỉ tài xế có thể tạo cuộc trò chuyện.")
    return {"conversation_id": runtime.sessions.create_conversation(principal.account_id)}


@router.post("/session/role")
async def select_legacy_role(selection: RoleSelection, response: Response):
    """Compatibility hook for the existing test suite; unavailable outside tests."""
    if runtime.settings.app_env != "test" or selection.role not in {"driver", "engineer"}:
        raise HTTPException(status_code=404, detail="Không tìm thấy endpoint.")
    phone = "+84900000001" if selection.role == "driver" else "+84900000002"
    if selection.role == "engineer":
        runtime.sessions.provision_engineer(phone, "0000")
    token, _ = runtime.sessions.login(phone, "0000")
    _set_cookie(response, token)
    return {"role": selection.role, "redirect": "/ivi" if selection.role == "driver" else "/engineer"}


@router.delete("/session")
async def clear_session(request: Request, response: Response):
    runtime.sessions.logout(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
