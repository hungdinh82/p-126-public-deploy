"""Small destinations in Oct 14 logs, with synthetic map coordinates and no network."""

from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import SessionState
from eval.legacy.grammar import OfflineParser, place_query_matches
from src.vivi.navigation.destinations import landmark_query
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.selection import exact_destination
from src.vivi.navigation.vietmap import Place, Route
from src.vivi.text import normalize_text
from tests.support.default_destination_review import UnavailableModel
from tests.support.dialogue_v2 import build, invoke
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
            "tieu hoc vo thi sau": ["Tiểu Học Võ Thị Sáu Ngõ 339 Minh Khai", "Trường Tiểu Học Võ Thị Sáu Trần Hưng Đạo"],
            "den tran, nam dinh": ["Đền Trần"],
            "trang an": ["Tràng An"],
        }.get(query, [])
        return [Place(ref_id=f"fixture:{i}:{query}", name=name,
                      address="Phường Nam Định, Nam Định" if "nam dinh" in query else "Phường Hoàn Kiếm, Hà Nội",
                      lat=21 + i * .0002, lng=105) for i, name in enumerate(names)]

    async def route(self, a, b):
        return Route(distance_m=2000, duration_s=300, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


def runtime(services, tmp_path):
    maps = PoiMaps()
    model = UnavailableModel()
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())),
                              tmp_path, model, local=model)
    gateway.vehicle.set_energy("driver", 82, 246)
    return graph, maps, model, gateway


async def located(graph, text, turn="one"):
    return await graph.ainvoke({"session_id": "driver", "turn_id": turn, "input_text": text,
                               "position": {"lat": 21.01, "lng": 105}})


@pytest.mark.parametrize("text,query", [
    ("Tôi muốn đến Hồ Gươm.", "Hồ Gươm"),
    ("Tôi muốn đến Hồ Tây.", "Hồ Tây"),
    ("Tôi muốn đến lăng Bác.", "lăng Bác"),
    ("chở tôi tới Hồ Gươm", "Hồ Gươm"),
    ("đưa mình đến trường tiểu học Trần Quốc Toản, Nam Định", "trường tiểu học Trần Quốc Toản, Nam Định"),
    ("Tôi muốn đi đến Nhà Tù Hỏa Lò nhé.", "Nhà Tù Hỏa Lò"),
])
def test_natural_destination_intent_is_not_a_handbook_question(text, query):
    semantic = OfflineParser().interpret(text, SessionState())
    assert semantic.act == "request" and len(semantic.actions) == 1
    assert semantic.actions[0].capability == "navigation.plan"
    assert semantic.actions[0].arguments.query == query


@pytest.mark.parametrize("text,name", [
    ("Tôi muốn đến Hồ Gươm.", "Hồ Hoàn Kiếm"),
    ("Chỉ đường tới hồ Hoàn Kiếm.", "Hồ Hoàn Kiếm"),
    ("Tôi muốn đến Hồ Tây.", "Hồ Tây"),
    ("Tôi muốn đến lăng Bác.", "Lăng Chủ tịch Hồ Chí Minh"),
    ("chở tôi tới Hồ Gươm", "Hồ Hoàn Kiếm"),
    ("Chỉ đường tới quảng trường Ba Đình.", "Quảng Trường Ba Đình"),
    ("Chỉ đường tới Vinhomes Times City.", "Vinhomes Times City"),
])
@pytest.mark.asyncio
async def test_logged_named_venue_produces_preview_without_model_wait_or_driving(services, tmp_path, text, name):
    graph, maps, model, gateway = runtime(services, tmp_path)
    try:
        result = await located(graph, text)
        assert result["navigation"]["selected"]["name"] == name
        assert result["navigation"]["status"] == "preview"
        assert result["navigation"]["offer"]["status"] == "pending"
        assert result["navigation"]["selected"]["selection_decision"]["source"] == "rules"
        assert model.calls == 0 and not gateway.vehicle.state_for("driver").driving
        assert result["output"]["diagnostics"]["provider_attempts"][0]["source"] == "explicit_navigation"
        if "Gươm" in text:
            assert maps.queries == ["Hồ Hoàn Kiếm, Hà Nội"]
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_actual_school_branches_still_require_selection(services, tmp_path):
    graph, _, model, gateway = runtime(services, tmp_path)
    try:
        result = await located(graph, "Chỉ đường tới tiểu học Võ Thị Sáu.")
        assert len(result["navigation"]["candidates"]) == 2
        assert result["navigation"]["selected"] is None and result["status"] == "clarify"
        assert model.calls == 0 and not gateway.vehicle.state_for("driver").driving
        selected = await invoke(graph, "chọn số 2", "select")
        assert selected["navigation"]["selected"]["name"] == "Trường Tiểu Học Võ Thị Sáu Trần Hưng Đạo"
        assert selected["navigation"]["status"] == "preview" and not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.asyncio
async def test_area_answer_keeps_requested_temple_instead_of_region_default(services, tmp_path, owner):
    graph, maps, _, gateway = runtime(services, tmp_path)
    graph.default_provider = "rules"
    try:
        text = "chỉ đường tới đền Trần" if owner == "navigation" else "lập kế hoạch tới đền Trần"
        first = await located(graph, text)
        state = first["navigation" if owner == "navigation" else "trip_plan"]
        assert state["clarification"]["phase"] == "place_area"
        assert state["clarification"]["query"] == "đền Trần"
        parsed = OfflineParser().interpret("Nam Định", SessionState.model_validate(first["session"]))
        assert parsed.actions[0].capability == f"{owner}.refine"
        assert parsed.actions[0].arguments.query == "đền Trần, Nam Định"
        second = await invoke(graph, "Nam Định", "area")
        assert maps.queries == ["đền Trần", "đền Trần, Nam Định"]
        state = second["navigation" if owner == "navigation" else "trip_plan"]
        assert len(state["candidates"]) == 1 and state["candidates"][0]["name"] == "Đền Trần"
        assert len(second["steps"]) == 1 and not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_failed_new_poi_keeps_live_route_without_reusing_old_destination_choice(services, tmp_path):
    graph, maps, _, gateway = runtime(services, tmp_path)
    graph.default_provider = "rules"
    try:
        await located(graph, "đi Tràng An")
        await invoke(graph, "đúng", "select")
        running = await invoke(graph, "đồng ý", "start")
        assert running["navigation"]["status"] == "running"
        failed = await invoke(graph, "chỉ đường tới đền Trần", "new")
        assert failed["navigation_workspace"]["journey"]["route_id"] == running["navigation"]["route_id"]
        assert failed["navigation"]["candidates"] == []
        assert failed["navigation"]["clarification"]["query"] == "đền Trần"
        clarified = await invoke(graph, "Nam Định", "area")
        assert maps.queries[-1] == "đền Trần, Nam Định"
        assert clarified["navigation_workspace"]["journey"]["route_id"] == running["navigation"]["route_id"]
        assert gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


def candidate(name, lat=21, address="Hà Nội"):
    return {"handle": name, "name": name, "address": address, "lat": lat, "lng": 105}


@pytest.mark.parametrize("other", [
    candidate("Ga Hải Phòng", 21.02), candidate("Ga Hải Phòng cửa Đông"),
    candidate("Ga Hải Phòng chi nhánh 2"), candidate("Ga Hải Phòng Phố A", address="Hải Phòng"),
])
def test_exact_match_cannot_resolve_branches_entrances_or_another_locality(other):
    assert exact_destination("Ga Hải Phòng", [candidate("Ga Hải Phòng"), other]) is None


def test_landmark_alias_does_not_rewrite_business_or_explicit_different_region():
    assert landmark_query("Hồ Gươm Plaza") == "Hồ Gươm Plaza"
    assert landmark_query("Hồ Gươm, Nam Định") == "Hồ Gươm, Nam Định"
    assert not place_query_matches("Trường tiểu học Trần Quốc Toản, Nam Định",
                                   candidate("Trường Tiểu Học Trần Quốc Toản", address="Hà Nội"))


@pytest.mark.parametrize("text", [
    "Chỉ đường tới Hồ Gươm ngày mai", "nếu trời đẹp thì tôi muốn đến Hồ Tây",
    "Chỉ đường tới Hồ Gươm rồi bật nhạc", "Chỉ đường tới quán cà phê", "Chào tôi tới Hồ Gươm.",
])
def test_shortcut_does_not_guess_stt_or_drop_scheduling_and_compound_actions(text):
    assert OfflineParser().explicit_navigation_reference(text, SessionState()) is None
