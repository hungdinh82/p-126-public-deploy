"""Nearest searches rank POIs around the car, not by provider text relevance."""

import pytest

from src.vivi.navigation.categories import matches_category, reconcile_category, strip_nearest
from src.vivi.navigation.contracts import DestinationSpec
from src.vivi.navigation.presentation import choice_message
from src.vivi.navigation.resolver import PlaceResolver
from src.vivi.navigation.vietmap import Place, Route

ORIGIN = {"lat": 21.0, "lng": 105.8}


def place(ref, name, km_north, categories=("10013", "10013-1")):
    return Place(ref_id=ref, name=name, lat=21.0 + km_north / 111.2, lng=105.8, categories=list(categories))


class Maps:
    """Autocomplete honours circle_radius; text relevance puts far places first."""

    def __init__(self, places):
        self.places, self.searches, self.routes = places, [], 0

    async def search(self, text, near=None, category=None, *, radius_m=None, layers=None):
        self.searches.append({"text": text, "category": category, "radius_m": radius_m, "layers": layers})
        found = [p for p in self.places if radius_m is None or (p.lat - 21.0) * 111200 <= radius_m]
        return sorted(found, key=lambda p: -p.lat)[:10]

    async def matrix(self, points):
        return [abs(lat - 21.0) * 111200 * 1.3 for lat, _ in points[1:]]

    async def route(self, origin, destination):
        self.routes += 1
        distance = abs(destination[0] - origin[0]) * 111200 * 1.3
        return Route(
            distance_m=distance,
            duration_s=distance / 8,
            coordinates=[[origin[1], origin[0]], [destination[1], destination[0]]],
            steps=[],
        )


@pytest.mark.asyncio
async def test_nearest_station_within_two_km_beats_far_text_match():
    maps = Maps(
        [
            place("far", "Trạm Sạc Điện Ô Tô VinFast Ocean Park", 16),
            place("mid", "Trạm Sạc Điện Ô Tô VinFast Nam Phù", 9),
            place("near", "Trạm Sạc Điện Ô Tô VinFast Times City", 0.3),
            place("near2", "Trạm Sạc Charge+ Autocare", 1.1),
            place("bike", "Trạm Sạc Xe Máy VinFast", 0.1),
            place("cafe", "Kaffe Trạm Sạc", 0.2, ("1001",)),
        ]
    )
    destination = DestinationSpec.from_arguments({"query": "trạm sạc", "category": "charging", "nearest": True})
    result = await PlaceResolver(maps).resolve(destination, ORIGIN, with_travel=True)
    assert [c["ref_id"] for c in result.candidates] == ["near", "near2", "mid"]
    assert maps.searches[0] == {"text": "trạm sạc", "category": "10013-1", "radius_m": 2000, "layers": "POI"}
    assert [s["radius_m"] for s in maps.searches] == [2000, 5000, 15000]
    assert maps.routes == 3
    assert "Times City, 400 m" in choice_message(result.candidates)


@pytest.mark.asyncio
async def test_empty_nearby_search_reports_radius_instead_of_asking_area_blindly():
    maps = Maps([place("far", "Bệnh Viện Xa", 80, ("7003",))])
    destination = DestinationSpec.from_arguments({"query": "bệnh viện gần nhất"})
    result = await PlaceResolver(maps).resolve(destination, ORIGIN, with_travel=True)
    assert result.status == "no_verified_match" and result.reason == "no_nearby_candidates"
    assert result.evidence == [{"radius_m": 30000}]


def test_spoken_query_overrides_a_mismatched_model_category():
    cafe = DestinationSpec.from_arguments({"query": "quán cà phê", "category": "charging", "nearest": True})
    assert cafe.entity_type == "cafe" and cafe.selection == "category"
    hospital = DestinationSpec.from_arguments({"query": "bệnh viện gần nhất"})
    assert (hospital.entity_type, hospital.nearest, hospital.query()) == ("hospital", True, "bệnh viện")


def test_named_place_is_not_turned_into_a_category_search():
    named = DestinationSpec.from_arguments({"query": "Bệnh viện Bạch Mai"})
    assert named.selection == "named_place" and not named.nearest
    assert reconcile_category("Bệnh viện Bạch Mai") is None
    assert reconcile_category("Highlands Coffee", nearest=True) == "cafe"


def test_category_identity_rejects_other_kinds_sharing_a_name():
    assert matches_category({"name": "Cây Xăng Phú Thụy", "categories": ["10009"]}, "gas_station")
    assert not matches_category({"name": "Kaffe Cây Xăng", "categories": ["1001", "1001-1"]}, "gas_station")
    assert not matches_category({"name": "Nhà Thuốc Bệnh Viện Dệt May", "categories": ["3004"]}, "hospital")
    assert matches_category({"name": "Vinfast Trạm Sạc Điện", "categories": []}, "charging")


def test_strip_nearest_keeps_the_place_words():
    assert strip_nearest("bệnh viện gần nhất") == "bệnh viện"
    assert strip_nearest("Highlands Coffee gần đây nhất") == "Highlands Coffee"


class NamedMaps(Maps):
    async def search(self, text, near=None, category=None, *, radius_m=None, layers=None):
        self.searches.append({"text": text, "layers": layers})
        if layers == "WARD":
            return [Place(ref_id="ward", name="Xã Tà Xùa", address="Xã Tà Xùa, Tỉnh Sơn La", lat=21.3, lng=104.4)]
        return [
            Place(
                ref_id="cafe",
                name="Tà Xùa Coffee Tea",
                address="Phường Phú Diễn, Thành phố Hà Nội",
                lat=21.05,
                lng=105.76,
            )
        ]


@pytest.mark.asyncio
async def test_commune_name_outranks_businesses_named_after_it():
    maps = NamedMaps([])
    result = await PlaceResolver(maps).resolve(DestinationSpec.from_arguments({"query": "Tà Xùa"}), ORIGIN)
    assert [c["ref_id"] for c in result.candidates] == ["ward", "cafe"]
    assert result.candidates[0]["spoken_label"] == "Xã Tà Xùa, Tỉnh Sơn La"


@pytest.mark.asyncio
async def test_spoken_accents_pick_phu_ly_over_phu_ly():
    class Wards(Maps):
        async def search(self, text, near=None, category=None, *, radius_m=None, layers=None):
            wards = [
                Place(ref_id="dongnai", name="Xã Phú Lý", address="Xã Phú Lý, Thành phố Đồng Nai", lat=11.2, lng=107.1),
                Place(
                    ref_id="ninhbinh",
                    name="Phường Phủ Lý",
                    address="Phường Phủ Lý, Tỉnh Ninh Bình",
                    lat=20.54,
                    lng=105.91,
                ),
            ]
            return wards if layers == "WARD" else wards[:1]

    result = await PlaceResolver(Wards([])).resolve(DestinationSpec.from_arguments({"query": "Phủ Lý"}), ORIGIN)
    assert result.candidates[0]["ref_id"] == "ninhbinh"


@pytest.mark.asyncio
async def test_vietmap_rate_limit_is_retried_before_giving_up():
    import httpx

    from src.vivi.config import Settings
    from src.vivi.navigation.vietmap import VietMapClient

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[{"ref_id": "a", "name": "Hồ Gươm", "lat": 21.0, "lng": 105.85}])

    client = VietMapClient(Settings(vietmap_api_key="test"), transport=httpx.MockTransport(handler))
    assert [p.name for p in await client.search("Hồ Gươm")] == ["Hồ Gươm"]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_representative_point_keeps_spoken_accents():
    class Wards(Maps):
        async def search(self, text, near=None, category=None, *, radius_m=None, layers=None):
            if layers == "WARD":

                def ward(prefix, name):
                    # VietMap lists the ward itself among its boundaries (type 2).
                    return [{"type": 2, "name": name, "full_name": f"{prefix} {name}"}, {"type": 0, "name": "Việt Nam"}]

                return [
                    Place(ref_id="dongnai", name="Xã Phú Lý", lat=11.2, lng=107.1, boundaries=ward("Xã", "Phú Lý")),
                    Place(
                        ref_id="ninhbinh",
                        name="Phường Phủ Lý",
                        lat=20.54,
                        lng=105.91,
                        boundaries=ward("Phường", "Phủ Lý"),
                    ),
                ]
            return []

    destination = DestinationSpec.from_arguments({"query": "Phủ Lý"})
    result = await PlaceResolver(Wards([])).resolve(destination, ORIGIN, representative=True)
    assert [c["ref_id"] for c in result.candidates][:1] == ["ninhbinh"]
