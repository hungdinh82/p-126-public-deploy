"""Small destinations in Oct 14 logs, with synthetic map coordinates and no network."""

from dataclasses import replace

import pytest

from src.vivi.navigation.destinations import landmark_query
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.poi import place_query_matches
from src.vivi.navigation.selection import exact_destination
from src.vivi.navigation.vietmap import Place, Route
from src.vivi.text import normalize_text
from tests.support.default_destination_review import UnavailableModel
from tests.support.dialogue_v2 import build
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile


class PoiMaps:
    def __init__(self):
        self.queries = []

    async def search(self, text, **kwargs):
        self.queries.append(text)
        query = normalize_text(text)
        names = {
            "ho hoan kiem, ha noi": ["Trung Tâm Thương Mại Hồ Gươm Plaza", "Phố Hồ Hoàn Kiếm", "Hồ Hoàn Kiếm"],
            "ho tay, ha noi": ["Hồ Tây", "Khách Sạn Hồ Tây"],
            "lang chu tich ho chi minh, ha noi": ["Lăng Chủ tịch Hồ Chí Minh"],
            "quang truong ba dinh": ["Quảng Trường Ba Đình Hùng Vương", "Quảng Trường Ba Đình"],
            "vinhomes times city": ["Times City T10 Vinhomes Times City", "Vinhomes Times City"],
            "tieu hoc vo thi sau": [
                "Tiểu Học Võ Thị Sáu Ngõ 339 Minh Khai",
                "Trường Tiểu Học Võ Thị Sáu Trần Hưng Đạo",
            ],
            "den tran, nam dinh": ["Đền Trần"],
            "trang an": ["Tràng An"],
        }.get(query, [])
        return [
            Place(
                ref_id=f"fixture:{i}:{query}",
                name=name,
                address="Phường Nam Định, Nam Định" if "nam dinh" in query else "Phường Hoàn Kiếm, Hà Nội",
                lat=21 + i * 0.0002,
                lng=105,
            )
            for i, name in enumerate(names)
        ]

    async def route(self, a, b):
        return Route(distance_m=2000, duration_s=300, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


def runtime(services, tmp_path):
    maps = PoiMaps()
    model = UnavailableModel()
    graph, _, gateway = build(
        replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path, model, local=model
    )
    gateway.vehicle.set_energy("driver", 82, 246)
    return graph, maps, model, gateway


async def located(graph, text, turn="one"):
    return await graph.ainvoke(
        {"session_id": "driver", "turn_id": turn, "input_text": text, "position": {"lat": 21.01, "lng": 105}}
    )


def candidate(name, lat=21, address="Hà Nội"):
    return {"handle": name, "name": name, "address": address, "lat": lat, "lng": 105}


@pytest.mark.parametrize(
    "other",
    [
        candidate("Ga Hải Phòng", 21.02),
        candidate("Ga Hải Phòng cửa Đông"),
        candidate("Ga Hải Phòng chi nhánh 2"),
        candidate("Ga Hải Phòng Phố A", address="Hải Phòng"),
    ],
)
def test_exact_match_cannot_resolve_branches_entrances_or_another_locality(other):
    assert exact_destination("Ga Hải Phòng", [candidate("Ga Hải Phòng"), other]) is None


def test_landmark_alias_does_not_rewrite_business_or_explicit_different_region():
    assert landmark_query("Hồ Gươm Plaza") == "Hồ Gươm Plaza"
    assert landmark_query("Hồ Gươm, Nam Định") == "Hồ Gươm, Nam Định"
    assert not place_query_matches(
        "Trường tiểu học Trần Quốc Toản, Nam Định", candidate("Trường Tiểu Học Trần Quốc Toản", address="Hà Nội")
    )
