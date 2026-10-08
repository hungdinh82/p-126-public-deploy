from __future__ import annotations

import math
from typing import Any

import httpx
from pydantic import BaseModel

from src.vivi.config import Settings


class VietMapError(RuntimeError):
    """Upstream VietMap failure that the API reports as a gateway error."""


class Place(BaseModel):
    ref_id: str
    name: str
    address: str = ""
    lat: float | None = None
    lng: float | None = None


class RouteStep(BaseModel):
    text: str
    distance_m: float
    duration_s: float
    sign: int
    street: str = ""
    point_index: int = 0


class Route(BaseModel):
    distance_m: float
    duration_s: float
    # GeoJSON order: [lng, lat].
    coordinates: list[list[float]]
    bbox: list[float] | None = None
    steps: list[RouteStep]


def decode_polyline(encoded: str, precision: int = 5) -> list[list[float]]:
    """Decode a Google-style polyline into GeoJSON [lng, lat] pairs."""
    coordinates: list[list[float]] = []
    index = lat = lng = 0
    factor = 10**precision
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        coordinates.append([lng / factor, lat / factor])
    return coordinates


class VietMapClient:
    """Thin async wrapper over VietMap autocomplete, place and route APIs."""

    def __init__(self, config: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = config.vietmap_api_key
        self.tile_key = config.vietmap_tile_key or config.vietmap_api_key
        self.base_url = config.vietmap_base_url.rstrip("/")
        self.map_style = config.vietmap_map_style
        self.timeout = config.vietmap_timeout_seconds
        self._transport = transport

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def style_url(self) -> str:
        return f"{self.base_url}/maps/styles/{self.map_style}/style.json?apikey={self.tile_key}"

    async def probe_tiles(self, lat: float, lng: float, zoom: int = 14) -> int:
        """Fetch one vector tile and return its HTTP status.

        Some VietMap keys are allowed to search and route but not to load map
        tiles (HTTP 423). The browser SDK hides that failure, so the UI asks the
        backend instead of showing a silently blank map.
        """
        style = await self._get(f"/maps/styles/{self.map_style}/style.json", [], key=self.tile_key)
        templates = [
            source["tiles"][0]
            for source in style.get("sources", {}).values()
            if source.get("type") == "vector" and source.get("tiles")
        ]
        if not templates:
            raise VietMapError("Style VietMap không có nguồn tile")
        scale = 2**zoom
        x = int((lng + 180) / 360 * scale)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * scale)
        url = templates[0].replace("{z}", str(zoom)).replace("{x}", str(x)).replace("{y}", str(y))
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                return (await client.get(url)).status_code
        except httpx.HTTPError as exc:
            raise VietMapError("Không kết nối được VietMap") from exc

    async def _get(self, path: str, params: list[tuple[str, str]], key: str | None = None) -> Any:
        query = [("apikey", key or self.api_key), *params]
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout, transport=self._transport
            ) as client:
                response = await client.get(path, params=query)
        except httpx.HTTPError as exc:
            raise VietMapError("Không kết nối được VietMap") from exc
        if response.status_code != 200:
            raise VietMapError(f"VietMap trả về HTTP {response.status_code}")
        return response.json()

    async def search(self, text: str, near: tuple[float, float] | None = None) -> list[Place]:
        params = [("text", text), ("display_type", "1")]
        if near:
            params.append(("focus", f"{near[0]},{near[1]}"))
        payload = await self._get("/api/autocomplete/v4", params)
        if not isinstance(payload, list):
            raise VietMapError("Phản hồi tìm kiếm VietMap không hợp lệ")
        return [
            Place(
                ref_id=item["ref_id"],
                name=item.get("name") or item.get("display") or "",
                address=item.get("address") or "",
            )
            for item in payload
            if item.get("ref_id")
        ]

    async def place(self, ref_id: str) -> Place:
        payload = await self._get("/api/place/v4", [("refid", ref_id)])
        if not isinstance(payload, dict) or payload.get("lat") is None:
            raise VietMapError("Không tìm thấy toạ độ địa điểm")
        return Place(
            ref_id=ref_id,
            name=payload.get("name") or payload.get("display") or "",
            address=payload.get("address") or payload.get("display") or "",
            lat=float(payload["lat"]),
            lng=float(payload["lng"]),
        )

    async def route(self, origin: tuple[float, float], destination: tuple[float, float]) -> Route:
        params = [
            ("api-version", "1.1"),
            ("point", f"{origin[0]},{origin[1]}"),
            ("point", f"{destination[0]},{destination[1]}"),
            ("vehicle", "car"),
            ("points_encoded", "false"),
        ]
        payload = await self._get("/api/route", params)
        paths = payload.get("paths") if isinstance(payload, dict) else None
        if not paths:
            raise VietMapError("VietMap không tìm được tuyến đường")
        path = paths[0]
        points = path.get("points")
        coordinates = decode_polyline(points) if isinstance(points, str) else points["coordinates"]
        return Route(
            distance_m=path["distance"],
            duration_s=path["time"] / 1000,
            coordinates=coordinates,
            bbox=path.get("bbox"),
            steps=[
                RouteStep(
                    text=" ".join(step.get("text", "").split()),
                    distance_m=step.get("distance", 0),
                    duration_s=step.get("time", 0) / 1000,
                    sign=step.get("sign", 0),
                    street=step.get("street_name") or "",
                    point_index=(step.get("interval") or [0])[0],
                )
                for step in path.get("instructions", [])
            ],
        )
