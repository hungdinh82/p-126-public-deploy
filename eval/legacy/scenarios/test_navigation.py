from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.vivi.api.runtime import runtime
from src.vivi.config import Settings
from src.vivi.navigation import VietMapClient, VietMapError
from src.vivi.navigation.vietmap import decode_polyline

ROUTE_PAYLOAD = {
    "code": "OK",
    "paths": [
        {
            "distance": 1520.4,
            "time": 300000,
            "bbox": [105.84, 21.02, 105.86, 21.03],
            "points": {"type": "LineString", "coordinates": [[105.85, 21.02], [105.852, 21.025], [105.86, 21.03]]},
            "instructions": [
                {
                    "distance": 800,
                    "time": 150000,
                    "sign": 0,
                    "text": "Đi thẳng",
                    "street_name": "Đinh Tiên Hoàng",
                    "interval": [0, 1],
                },
                {
                    "distance": 720,
                    "time": 150000,
                    "sign": 2,
                    "text": "Rẽ phải",
                    "street_name": "Hàng Khay",
                    "interval": [1, 2],
                },
                {"distance": 0, "time": 0, "sign": 4, "text": "Đến nơi", "interval": [2, 2]},
            ],
        }
    ],
}


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["apikey"] == "test-key"
    if request.url.path == "/api/autocomplete/v4":
        return httpx.Response(
            200,
            json=[
                {"ref_id": "ref-1", "name": "Hồ Gươm", "address": "Hoàn Kiếm, Hà Nội"},
                {"name": "thiếu ref_id"},
            ],
        )
    if request.url.path == "/api/place/v4":
        return httpx.Response(200, json={"name": "Hồ Gươm", "address": "Hoàn Kiếm", "lat": 21.0287, "lng": 105.8524})
    if request.url.path == "/api/route":
        assert request.url.params.get_list("point") == ["21.02,105.85", "21.03,105.86"]
        return httpx.Response(200, json=ROUTE_PAYLOAD)
    return httpx.Response(404)


def _client(handler=_handler) -> VietMapClient:
    return VietMapClient(Settings(vietmap_api_key="test-key"), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_search_skips_results_without_reference():
    places = await _client().search("ho guom", near=(21.0, 105.8))
    assert [place.ref_id for place in places] == ["ref-1"]


@pytest.mark.asyncio
async def test_route_is_normalized_for_ui():
    route = await _client().route((21.02, 105.85), (21.03, 105.86))
    assert route.duration_s == 300
    assert route.coordinates[0] == [105.85, 21.02]
    assert [step.point_index for step in route.steps] == [0, 1, 2]
    assert route.steps[1].street == "Hàng Khay"


@pytest.mark.asyncio
async def test_upstream_error_raises_vietmap_error():
    client = _client(lambda request: httpx.Response(401, json={"message": "invalid key"}))
    with pytest.raises(VietMapError):
        await client.place("ref-1")


def test_decode_polyline_returns_lng_lat_pairs():
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]


@pytest.mark.asyncio
async def test_navigation_config_reports_missing_key(client):
    with patch.object(runtime.settings, "vietmap_api_key", ""):
        response = await client.get("/api/v1/navigation/config")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["style_url"] is None


@pytest.mark.asyncio
async def test_navigation_endpoints_require_key(client):
    with patch.object(runtime.settings, "vietmap_api_key", ""):
        response = await client.get("/api/v1/navigation/search", params={"text": "Hồ Gươm"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_route_endpoint_rejects_malformed_coordinates(client):
    with patch.object(runtime.settings, "vietmap_api_key", "test-key"):
        response = await client.get("/api/v1/navigation/route", params={"origin": "abc", "destination": "21,105"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_route_endpoint_proxies_vietmap(client):
    transport = httpx.MockTransport(_handler)
    original = VietMapClient.__init__

    def with_mock(self, config, transport_=None):
        original(self, config, transport=transport)

    with (
        patch.object(runtime.settings, "vietmap_api_key", "test-key"),
        patch.object(VietMapClient, "__init__", with_mock),
    ):
        response = await client.get(
            "/api/v1/navigation/route", params={"origin": "21.02,105.85", "destination": "21.03,105.86"}
        )
    assert response.status_code == 200
    assert response.json()["distance_m"] == 1520.4


@pytest.mark.asyncio
async def test_probe_tiles_reports_rejected_tile_key():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("style.json"):
            return httpx.Response(
                200,
                json={
                    "sources": {
                        "omt": {"type": "vector", "tiles": ["https://tiles.test/{z}/{x}/{y}.pbf?apikey=test-key"]}
                    }
                },
            )
        assert request.url.path == "/14/13009/7212.pbf"
        return httpx.Response(423, text="Your request is limited")

    assert await _client(handler).probe_tiles(21.0285, 105.8522) == 423


def test_style_url_prefers_tilemap_key():
    client = VietMapClient(Settings(vietmap_api_key="api-key", vietmap_tile_key="tile-key"))
    assert client.style_url.endswith("apikey=tile-key")
    assert VietMapClient(Settings(vietmap_api_key="api-key", vietmap_tile_key="")).style_url.endswith("apikey=api-key")
