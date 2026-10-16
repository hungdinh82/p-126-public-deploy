from __future__ import annotations

import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.vivi.api.contracts import envelope
from src.vivi.api.runtime import runtime
from src.vivi.domain.models import TurnRequest
from src.vivi.navigation import Place, Route, VietMapClient, VietMapError
from src.vivi.navigation.workflow import NavigationEvent

router = APIRouter(prefix="/api/v2/navigation")


@router.get("/snapshot")
async def navigation_snapshot(
    session_id: str = Query(min_length=1, max_length=100), event_id: str | None = Query(default=None, max_length=100)
):
    return envelope(await runtime.orchestrator.navigation_snapshot(session_id, event_id))


class NavigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)
    event: NavigationEvent
    known_routes: list[str] = Field(default_factory=list, max_length=10)


@router.post("/events")
async def navigation_event(request: NavigationRequest):
    try:
        turn = TurnRequest(
            session_id=request.session_id,
            turn_id=str(uuid4()),
            transcript="Cập nhật hành trình",
            navigation_event=request.event.model_dump(mode="json"),
        )
        response = await runtime.orchestrator.run(turn)
        payload = envelope(response)
        if request.event.kind in {"progress", "charge"}:
            from src.vivi.navigation.transport import compact_snapshot

            payload = compact_snapshot(payload, set(request.known_routes))
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Quá thời gian cập nhật hành trình.") from exc


def _client() -> VietMapClient:
    if not runtime.connectivity.capability_available("rag", "cloud"):
        raise HTTPException(
            status_code=503,
            detail="Đang offline: tuyến hiện tại vẫn hoạt động nhưng không thể tìm hoặc lập tuyến mới.",
        )
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
        "tile_status": _tile_probe[2] if _tile_probe and _tile_probe[0] == client.tile_key else None,
        "default_center": {"lat": lat, "lng": lng, "label": config.navigation_default_label},
    }


@router.get("/map-health")
async def map_health():
    client = _client()
    try:
        status = await _tile_status(
            client, runtime.settings.navigation_default_lat, runtime.settings.navigation_default_lng
        )
        return {"tile_status": status}
    finally:
        await client.aclose()


@router.get("/search", response_model=list[Place])
async def search_places(
    text: str = Query(min_length=1, max_length=200),
    near: str | None = Query(default=None),
):
    try:
        client = _client()
        try:
            return await client.search(text, _coordinate(near) if near else None)
        finally:
            await client.aclose()
    except VietMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/place/{ref_id}", response_model=Place)
async def place_detail(ref_id: str):
    try:
        client = _client()
        try:
            return await client.place(ref_id)
        finally:
            await client.aclose()
    except VietMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/route", response_model=Route)
async def plan_route(origin: str = Query(...), destination: str = Query(...)):
    try:
        client = _client()
        try:
            return await client.route(_coordinate(origin), _coordinate(destination))
        finally:
            await client.aclose()
    except VietMapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
