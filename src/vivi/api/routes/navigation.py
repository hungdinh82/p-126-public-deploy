from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from src.vivi.api.runtime import runtime
from src.vivi.navigation import Place, Route, VietMapClient, VietMapError

router = APIRouter(prefix="/api/v1/navigation")


def _client() -> VietMapClient:
    client = VietMapClient(runtime.settings)
    if not client.available:
        raise HTTPException(status_code=503, detail="Chưa cấu hình VIETMAP_API_KEY trong .env")
    return client


def _coordinate(value: str) -> tuple[float, float]:
    try:
        lat, lng = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Toạ độ phải có dạng lat,lng") from exc
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=422, detail="Toạ độ nằm ngoài phạm vi")
    return lat, lng


# (tile_key, checked_at, status); avoids probing VietMap on every panel open.
_tile_probe: tuple[str, float, int | None] | None = None
TILE_PROBE_TTL_SECONDS = 600


async def _tile_status(client: VietMapClient, lat: float, lng: float) -> int | None:
    global _tile_probe
    if _tile_probe and _tile_probe[0] == client.tile_key and time.monotonic() - _tile_probe[1] < TILE_PROBE_TTL_SECONDS:
        return _tile_probe[2]
    try:
        status = await client.probe_tiles(lat, lng)
    except VietMapError:
        status = None
    _tile_probe = (client.tile_key, time.monotonic(), status)
    return status


@router.get("/config")
async def navigation_config():
    config = runtime.settings
    client = VietMapClient(config)
    lat, lng = config.navigation_default_lat, config.navigation_default_lng
    return {
        "available": client.available,
        # The browser fetches tiles directly, so the style URL carries the key.
        "style_url": client.style_url if client.available else None,
        "tile_status": await _tile_status(client, lat, lng) if client.available else None,
        "default_center": {"lat": lat, "lng": lng, "label": config.navigation_default_label},
    }


@router.get("/search", response_model=list[Place])
async def search_places(
    text: str = Query(min_length=1, max_length=200),
    near: str | None = Query(default=None),
):
    try:
        return await _client().search(text, _coordinate(near) if near else None)
    except VietMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/place/{ref_id}", response_model=Place)
async def place_detail(ref_id: str):
    try:
        return await _client().place(ref_id)
    except VietMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/route", response_model=Route)
async def plan_route(origin: str = Query(...), destination: str = Query(...)):
    try:
        return await _client().route(_coordinate(origin), _coordinate(destination))
    except VietMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
