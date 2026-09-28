from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parents[4]


@router.get("/")
async def index():
    return FileResponse(PROJECT_ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/{asset_name}")
async def asset(asset_name: str):
    if asset_name not in {"app.js", "style.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(PROJECT_ROOT / asset_name, headers={"Cache-Control": "no-store"})
