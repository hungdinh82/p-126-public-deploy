from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from src.vivi.api.routes.roles import role_for

router = APIRouter()
PROJECT_ROOT = Path(__file__).resolve().parents[4]


@router.get("/")
async def index():
    return FileResponse(PROJECT_ROOT / "role.html", headers={"Cache-Control": "no-store"})


@router.get("/ivi")
async def ivi(request: Request):
    if role_for(request) != "driver":
        return RedirectResponse("/", status_code=303)
    return FileResponse(PROJECT_ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/engineer")
async def engineer(request: Request):
    if role_for(request) != "engineer":
        return RedirectResponse("/", status_code=303)
    return FileResponse(PROJECT_ROOT / "engineer.html", headers={"Cache-Control": "no-store"})


@router.get("/record")
async def record():
    """Voice recorder for wake-word clips; needs no role, so it can be shared as a link."""
    return FileResponse(PROJECT_ROOT / "record.html", headers={"Cache-Control": "no-store"})


MODEL_ASSETS = {"vf8.glb", "vf8-hq.glb", "vf8-lossless.glb"}


@router.get("/assets/{asset_name}")
async def model_asset(asset_name: str):
    if asset_name not in MODEL_ASSETS:
        raise HTTPException(status_code=404)
    return FileResponse(
        PROJECT_ROOT / "assets" / asset_name,
        media_type="model/gltf-binary",
        headers={"Cache-Control": "public, max-age=86400"},
    )


WAKE_ASSETS = {"melspectrogram.onnx", "embedding_model.onnx", "vivi.onnx"}


@router.get("/wake/{asset_name}")
async def wake_asset(asset_name: str):
    """Wake-word models for wake.js; vivi.onnx is replaced after each retraining, so revalidate."""
    if asset_name not in WAKE_ASSETS:
        raise HTTPException(status_code=404)
    return FileResponse(
        PROJECT_ROOT / "assets" / "wake" / asset_name,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/{asset_name}")
async def asset(asset_name: str):
    if asset_name == "monitor":
        return FileResponse(PROJECT_ROOT / "monitor.html", headers={"Cache-Control": "no-store"})
    if asset_name not in {"app.js", "journey.js", "wake.js", "music.js", "scene3d.js", "wave.js", "neon.js", "style.css", "monitor.js", "engineer.js", "engineer.css", "role.js", "role.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(PROJECT_ROOT / asset_name, headers={"Cache-Control": "no-store"})
