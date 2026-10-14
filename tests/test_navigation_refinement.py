"""Focused destination-refinement and real-POI charging-estimate scenarios."""

from dataclasses import replace
from pathlib import Path

import pytest

from src.vivi.dialogue.contracts import NavigationState, SemanticTurn, SessionState
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.navigation.charging import VietMapChargingDiscovery
from src.vivi.navigation.destinations import region_for
from src.vivi.navigation.energy import EnergyPlanner, load_assumptions
from src.vivi.navigation.vietmap import Place, Route
from src.vivi.navigation.workflow import NavigationWorkflow
from tests.test_charging_discovery import DEST, ORIGIN, Maps, equipment, road
from tests.test_dialogue_v2 import ScriptedModel, build, invoke
from tests.test_long_term_memory import services as services
from tests.test_navigation_workflow import profile


@pytest.mark.parametrize("query", ["TP.HCM", "TP. HCM", "TPHCM", "trung tâm thành phố Hồ Chí Minh"])
def test_hcm_abbreviations_and_explicit_center_are_regions(query):
    assert region_for(query)["name"] == "Hồ Chí Minh"
    assert region_for("Sport House Tp Hcm") is None


def test_new_city_center_cannot_select_an_unrelated_previous_poi():
    parser = OfflineParser()
    session = SessionState(
        navigation=NavigationState(
            status="selecting",
            candidates=[
                {"handle": "place:sport-house", "name": "Sport House Tp Hcm", "address": "Phường TEST"},
            ],
        )
    )
    semantic = SemanticTurn.model_validate(
        {
            "act": "request",
            "actions": [
                {"capability": "navigation.select", "arguments": {"place_handle": "place:sport-house"}},
            ],
        }
    )
    result = parser.guard("Đưa tôi đến trung tâm thành phố Hồ Chí Minh.", semantic, session)
    assert result.actions[0].capability == "navigation.plan"
    assert result.actions[0].arguments.destination_scope == "region"
    assert not result.actions[0].arguments.place_handle


@pytest.mark.asyncio
async def test_log_hcm_search_and_center_use_city_evidence_without_businesses(services, tmp_path):
    class CityMaps(DestinationMaps):
        async def search(self, text, **kwargs):
            self.queries.append((text, kwargs))
            assert text == "Hồ Chí Minh" and kwargs["layers"] == "CITY"
            return [
                Place(
                    ref_id="hcm",
                    name="Thành phố Hồ Chí Minh",
                    address="Việt Nam",
                    lat=10.77,
                    lng=106.7,
                    boundaries=[{"name": "Hồ Chí Minh", "full_name": "Thành phố Hồ Chí Minh"}],
                )
            ]

    maps = CityMaps()
    model = ScriptedModel(
        [
            {"act": "request", "actions": [{"capability": "navigation.search", "arguments": {"query": "TP.HCM"}}]},
            {"act": "reply", "text": "Được nhé"},
        ]
    )
    graph, _, _ = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path, model)
    graph.default_provider = "google"
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "city",
                "input_text": "Hi ViVi, bây giờ tôi muốn đi TP.HCM.",
                "position": {"lat": 10.78, "lng": 106.69, "simulated": True},
            }
        )
        assert "Hồ Chí Minh" in first["response_text"] and "Sport House" not in first["response_text"]
        second = await invoke(graph, "Đưa tôi đến trung tâm thành phố Hồ Chí Minh.", "center")
        assert second["navigation"]["selected"]["ref_id"] == "hcm"
        assert second["navigation"]["status"] == "preview"
        assert second["navigation"]["offer"]
        assert not second["navigation"]["clarification"]
    finally:
        graph.close()


class DestinationMaps:
    def __init__(self):
        self.queries = []

    async def search(self, text, **kwargs):
        self.queries.append((text, kwargs))
        if kwargs.get("layers") == "WARD":
            return [
                Place(
                    ref_id="hoa-lu",
                    name="Phường Hoa Lư",
                    address="Tỉnh Ninh Bình",
                    lat=20.25,
                    lng=105.95,
                    boundaries=[
                        {"type": 2, "name": "Hoa Lư", "full_name": "Phường Hoa Lư"},
                        {"type": 0, "name": "Ninh Bình", "full_name": "Tỉnh Ninh Bình"},
                    ],
                )
            ]
        return [
            Place(ref_id="trang-an", name="Tràng An", address="Phường Hoa Lư, Tỉnh Ninh Bình", lat=20.25, lng=105.95)
        ]

    async def route(self, a, b):
        return Route(distance_m=80000, duration_s=4000, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_region_center_preserves_chain_and_requires_route_consent(services, tmp_path, tier):
    maps = DestinationMaps()
    services = replace(services, navigation=maps, energy_planner=EnergyPlanner(profile()))
    first = OfflineParser().interpret("đi Ninh Bình rồi bật nhạc", SessionState()).model_dump(exclude_none=True)
    model = ScriptedModel([first, {"act": "reply", "text": "Được nhé"}, {"act": "confirm", "task_ref": "active"}])
    graph, _, gateway = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "region",
                "input_text": "đi Ninh Bình rồi bật nhạc",
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            }
        )
        assert "trung tâm phường Hoa Lư" in result["response_text"]
        assert result["navigation"]["route"] is not None and maps.queries[0][0] == "Phường Hoa Lư, Ninh Bình"
        assert [s["status"] for s in result["steps"]] == ["waiting_slot", "pending"]
        if tier != "rules":
            center = result["navigation"]["clarification"]["choices"][0]["handle"]
            model.replies = iter(
                [
                    {
                        "act": "request",
                        "actions": [{"capability": "navigation.refine", "arguments": {"choice_handle": center}}],
                    },
                    {"act": "confirm", "task_ref": "active"},
                ]
            )
        selected = await invoke(graph, "trung tâm", "center")
        assert selected["status"] == "confirmation_required"
        assert selected["navigation"]["selected"]["representative"]
        assert selected["navigation"]["summary"]["stop_count"] == 0
        assert not gateway.vehicle.state_for("driver").driving
        assert not gateway.vehicle.state_for("driver").media_playing
        done = await invoke(graph, "đồng ý", "start")
        assert all(s["status"] == "completed" for s in done["steps"]), [
            (s["status"], s["result"]) for s in done["steps"]
        ]
        assert gateway.vehicle.state_for("driver").driving and gateway.vehicle.state_for("driver").media_playing
        assert done["navigation"]["status"] == "running" and not done["navigation"].get("offer")
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_specific_place_choice_and_named_reply_keep_region(services, tmp_path):
    maps = DestinationMaps()
    graph, _, _ = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "region",
                "input_text": "đi Ninh Bình",
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            }
        )
        reply = await invoke(graph, "chỗ khác", "other")
        assert reply["navigation"]["clarification"]["phase"] == "specific"
        reply = await invoke(graph, "Tràng An", "name")
        assert maps.queries[-1][0] == "Tràng An, Ninh Bình"
        assert reply["navigation"]["status"] == "selecting" and reply["navigation"]["selected"] is None
        reply = await invoke(graph, "Tràng An", "select-name")
        assert reply["status"] == "answered" and reply["navigation"]["offer"]["status"] == "pending"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_actual_map_station_can_use_explicit_assumption_or_verified_power(services):
    assumptions = load_assumptions(Path("data/energy/charging-assumptions.json"))
    for known in [False, True]:
        maps = Maps()
        provider = VietMapChargingDiscovery(maps, [equipment()] if known else [], assumptions=assumptions)
        runtime = replace(
            services, navigation=maps, energy_planner=EnergyPlanner(profile()), charging_discovery=provider
        )
        nav = NavigationState(
            origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
            selected={"lat": DEST[0], "lng": DEST[1]},
            route=road().model_dump(),
        )
        energy, _ = await NavigationWorkflow(runtime).plan_energy(nav, 50, "rules")
        stop = energy["selected_plan"]["stops"][0]
        assert stop["map_ref_id"] == "s1" and not stop["simulated"]
        assert stop["equipment_basis"] == ("verified" if known else "assumed")
        assert stop["power_kw"] == (100 if known else 120)
        assert stop["charge_minutes"] == pytest.approx(14)  # vehicle limited to 100 kW


@pytest.mark.parametrize("text", ["Ninh Bình", "tỉnh Ninh Bình", "Thanh Hoá", "Hải Phòng"])
def test_region_phrases_keep_vietnamese_and_require_clarification(text):
    action = OfflineParser().interpret("dẫn đường đến " + text, SessionState()).actions[0]
    assert action.arguments.query == text and action.arguments.destination_scope == "region"
