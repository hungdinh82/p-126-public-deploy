from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.vivi.api.routes.roles import principal_for, require_principal
from src.vivi.api.runtime import runtime
from src.vivi.memory.sqlite import MemoryValue, SQLiteLongTermMemory

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fact", "preference", "note", "command"]
    value: str | float
    label: str = Field(default="Ghi chú", min_length=1, max_length=100)


def _store() -> tuple[SQLiteLongTermMemory, str]:
    config = runtime.settings
    if not config.memory_enabled:
        raise HTTPException(status_code=409, detail="Bộ nhớ dài hạn đang tắt.")
    return SQLiteLongTermMemory(config.memory_path), config.memory_profile_id


def _scoped_store(request: Request) -> tuple[SQLiteLongTermMemory, str]:
    store, configured_profile = _store()
    config = runtime.settings
    if config.app_env == "test" or not config.auth_required:
        principal = principal_for(request)
        profile = principal.account_id if principal else configured_profile
    else:
        profile = require_principal(request).account_id
    return store, profile


@router.get("")
def list_memory(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    store, profile = _scoped_store(request)
    return {"profile_id": profile, "items": store.list(profile)}


@router.put("/{key}")
def remember(key: str, value: MemoryWrite, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    store, profile = _scoped_store(request)
    try:
        item = MemoryValue(key=key, **value.model_dump())
        return store.remember(profile, item)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("")
def reset_memory(request: Request):
    store, profile = _scoped_store(request)
    return {"profile_id": profile, "deleted": store.reset(profile)}


@router.delete("/{key}")
def forget(key: str, request: Request):
    store, profile = _scoped_store(request)
    if not store.forget(profile, key):
        raise HTTPException(status_code=404, detail="Chưa lưu thông tin này.")
    return {"profile_id": profile, "deleted": key}
