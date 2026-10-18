"""Live-map contracts with deterministic fixtures; no real station is invented."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from eval.legacy.grammar import OfflineParser, charging_poi_info
from src.vivi.config import Settings
from src.vivi.dialogue.contracts import NavigationState, SessionState
from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.navigation.charging import VietMapChargingDiscovery, distance, route_context, sample_points
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import Place, VietMapClient
from src.vivi.navigation.workflow import NavigationWorkflow
from tests.support.charging_discovery import DEST as DEST
from tests.support.charging_discovery import OPPOSITE as OPPOSITE
from tests.support.charging_discovery import ORIGIN as ORIGIN
from tests.support.charging_discovery import STOP as STOP
from tests.support.charging_discovery import STOP_NAME as STOP_NAME
from tests.support.charging_discovery import Maps as Maps
from tests.support.charging_discovery import equipment as equipment
from tests.support.charging_discovery import road as road
from tests.support.dialogue_v2 import build, invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile, progress, station


@pytest.mark.asyncio
async def test_vietmap_search_uses_a_bounded_poi_area():
    def handler(request):
        query = request.url.params
        assert query["circle_center"] == "20.5,105.85"
        assert query["circle_radius"] == "20000" and query["layers"] == "POI"
        assert query["cats"] == "verified-test-category"
        return httpx.Response(200, json=[])

    maps = VietMapClient(Settings(vietmap_api_key="test-only"), transport=httpx.MockTransport(handler))
    assert (
        await maps.search("trạm sạc", near=STOP, radius_m=20000, layers="POI", category="verified-test-category") == []
    )
    with pytest.raises(ValueError):
        await maps.search("trạm sạc", radius_m=20000)


def test_samples_cover_the_route_and_projection_is_not_an_official_km_marker():
    samples = sample_points(road().model_dump())
    assert len(samples) == 7 and samples[0] == ORIGIN
    assert samples[-1] == pytest.approx(DEST)
    gap, along, street = route_context(STOP, road().model_dump())
    assert gap < 4000 and 60000 < along < 100000 and street == "Cao tốc TEST"
    assert distance(ORIGIN, ORIGIN) == 0


def test_long_route_samples_have_no_large_gaps_between_search_circles():
    route = road((21, 106), (14.3, 106), 746000).model_dump()
    samples = sample_points(route)
    assert len(samples) == 23
    assert all(distance(a, b) < 35000 for a, b in zip(samples, samples[1:]))


@pytest.mark.asyncio
async def test_long_route_charging_plan_has_coverage_beyond_the_origin(services):
    from src.vivi.navigation.energy import ChargingAssumptions

    origin, destination = (21, 106), (14.3, 106)
    route = road(origin, destination, 746000)
    locations = [(21 - 6.7 * i / 22, 106) for i in range(23)]

    class CorridorMaps:
        async def search(self, text, *, near, **kwargs):
            if text == "trạm dừng nghỉ":
                return []
            index = min(range(len(locations)), key=lambda i: distance(near, locations[i]))
            rest = " Trạm Dừng Nghỉ" if index in {6, 14} else ""
            return [
                Place(
                    ref_id=f"corridor-{index}-{j}",
                    name=f"Trạm Sạc VinFast{rest} TEST {index}-{j}",
                    lat=locations[index][0],
                    lng=106,
                )
                for j in range(10 if index == 0 else 1)
            ]

        async def route(self, a, b):
            return road(a, b, abs(a[0] - b[0]) / 6.7 * 746000)

    maps = CorridorMaps()
    assumptions = ChargingAssumptions(
        power_kw=120, max_soc=80, networks=["vgreen"], source="Synthetic test equipment", updated_at="2026-10-12"
    )
    discovery = VietMapChargingDiscovery(maps, assumptions=assumptions)
    found = await discovery.discover(route.model_dump())
    assert len(found.stations) == 10
    assert any(250000 < s.route_progress_m < 500000 for s in found.stations)
    assert all(b.route_progress_m - a.route_progress_m < 190000 for a, b in zip(found.stations, found.stations[1:]))
    runtime = replace(services, navigation=maps, energy_planner=EnergyPlanner(profile()), charging_discovery=discovery)
    nav = NavigationState(
        origin={"lat": origin[0], "lng": origin[1]},
        selected={"lat": destination[0], "lng": destination[1]},
        route=route.model_dump(),
    )
    energy, plans = await NavigationWorkflow(runtime).plan_energy(nav, 82, "rules")
    assert energy["status"] == "planned" and plans, str(
        {
            k: energy.get(k)
            for k in ["status", "station_search", "routing_errors", "routing_incomplete", "search_limit_reached"]
        }
    )
    assert len(plans[0]["stops"]) >= 2
    assert all(s["arrival_soc"] >= 15 and s["departure_soc"] <= 80 for s in plans[0]["stops"])
    assert plans[0]["arrival_soc"] >= 15


def test_solver_can_visit_six_stations_before_the_final_leg():
    stations = [station(identifier=f"s{i}", lat=21 - i * 0.8, lng=106) for i in range(6)]
    nodes = ["origin", *(s.id for s in stations), "destination"]
    edges = {(a, b): road(length=100000).model_dump() for a, b in zip(nodes, nodes[1:])}
    planner = EnergyPlanner(profile())
    plans = planner.solve(ORIGIN, DEST, 82, edges, stations)
    assert plans and len(plans[0]["legs"]) == 7
    assert plans[0]["arrival_soc"] >= 15


@pytest.mark.asyncio
async def test_north_south_route_retains_full_charging_coverage_with_bounded_edges(services):
    from src.vivi.navigation.energy import ChargingAssumptions

    origin, destination = (21, 106), (6.6, 106)
    route = road(origin, destination, 1600000)
    samples = sample_points(route.model_dump())
    assert len(samples) == 47
    assert all(distance(a, b) <= 35000 for a, b in zip(samples, samples[1:]))

    class LongCorridorMaps:
        def __init__(self):
            self.roads = []

        async def search(self, text, *, near, **kwargs):
            if text != "trạm sạc":
                return []
            i = min(range(len(samples)), key=lambda i: distance(samples[i], near))
            return [Place(ref_id=f"long-{i}", name=f"Trạm Sạc VinFast TEST {i}", lat=samples[i][0], lng=samples[i][1])]

        async def route(self, a, b):
            self.roads.append((a, b))
            return road(a, b, abs(a[0] - b[0]) / 14.4 * 1600000)

    maps = LongCorridorMaps()
    assumptions = ChargingAssumptions(
        power_kw=120, max_soc=80, networks=["vgreen"], source="Test only", updated_at="2026-10-12"
    )
    discovery = VietMapChargingDiscovery(maps, assumptions=assumptions)
    found = await discovery.discover(route.model_dump())
    assert len(found.stations) == 19
    assert found.stations[-1].route_progress_m > 1550000
    assert all(b.route_progress_m - a.route_progress_m <= 105000 for a, b in zip(found.stations, found.stations[1:]))
    runtime = replace(services, navigation=maps, energy_planner=EnergyPlanner(profile()), charging_discovery=discovery)
    nav = NavigationState(
        origin={"lat": origin[0], "lng": origin[1]},
        selected={"lat": destination[0], "lng": destination[1]},
        route=route.model_dump(),
    )
    energy, plans = await NavigationWorkflow(runtime).plan_energy(nav, 82, "rules")
    assert energy["status"] == "planned" and plans
    plan = energy["selected_plan"]
    assert len(plan["stops"]) >= 7 and plan["arrival_soc"] >= 15
    assert all(stop["arrival_soc"] >= 15 and stop["departure_soc"] <= 80 for stop in plan["stops"])
    assert len(maps.roads) < 150
    assert not energy.get("search_limit_reached")


def test_thanhhoa_is_a_region_and_charging_ordinal_uses_the_station_list():
    parser = OfflineParser()
    result = parser.interpret("lộ trình tới Thanh Hoá", SessionState())
    assert result.actions[0].arguments.destination_scope == "region"
    session = SessionState(
        navigation=NavigationState(
            candidates=[{"handle": "destination-city"}],
            energy={"station_candidates": [{"handle": "charger"}]},
        )
    )
    selected = parser.interpret("đến trạm một", session)
    assert selected.actions[0].arguments.place_handle == "charger"


def test_model_context_keeps_station_handles_without_copying_road_polylines():
    route = road().model_dump()
    plan = {"id": "p1", "legs": [route], "arrival_soc": 15, "stops": [{"map_ref_id": "s1", "name": STOP_NAME}]}
    # A replacement route is only kept beside a live journey.
    nav = NavigationState(
        status="running",
        route=route,
        energy={"selected_plan": plan, "station_candidates": [{"handle": "charger"}]},
        alternatives=[plan],
        pending={"navigation": {"route": route, "energy": {"selected_plan": plan}}},
    )
    session = SessionState(navigation=nav)
    view = SemanticResolver.context(session)["navigation"]
    assert "coordinates" not in view["route"] and "legs" not in view["energy"]["selected_plan"]
    assert "legs" not in view["alternatives"][0] and "coordinates" not in view["pending"]["navigation"]["route"]
    assert view["energy"]["station_candidates"][0]["handle"] == "charger"
    assert session.navigation.route["coordinates"] == route["coordinates"]
    assert session.navigation.energy["selected_plan"]["legs"] == [route]


@pytest.mark.asyncio
async def test_rest_area_name_requires_a_separate_real_map_poi():
    class RestMaps(Maps):
        async def search(self, text, **kwargs):
            if text == "trạm dừng nghỉ":
                return [Place(ref_id="rest", name="Trạm Dừng Nghỉ Có Tên TEST", lat=STOP[0], lng=STOP[1] + 0.001)]
            return [Place(ref_id="s1", name="Trạm Sạc VinFast TEST", lat=STOP[0], lng=STOP[1])]

    result = await VietMapChargingDiscovery(RestMaps()).discover(road().model_dump())
    assert result.stations[0].rest_area_name == "Trạm Dừng Nghỉ Có Tên TEST"
    assert result.stations[0].rest_area_ref_id == "rest"
    assert result.stations[0].name == "Trạm Sạc VinFast TEST"
    assert result.stations[0].power_kw is None


@pytest.mark.parametrize(
    "name,expected",
    [
        (STOP_NAME, True),
        ("Trạm Sạc Điện Ô Tô VinFast TEST", True),
        ("Trạm Sạc V-Green TEST", True),
        ("Trạm Dừng Nghỉ TEST", False),
        ("Trạm Sạc Xe Máy VinFast TEST", False),
        ("Tủ Đổi Pin V-Green TEST", False),
    ],
)
def test_only_car_charging_identity_from_map_names(name, expected):
    assert bool(charging_poi_info(name)) == expected


@pytest.mark.asyncio
async def test_discovery_deduplicates_hydrates_and_keeps_real_map_identity():
    maps = Maps(detail=True)
    result = await VietMapChargingDiscovery(maps).discover(road().model_dump())
    assert len(result.stations) == 1
    found = result.stations[0]
    assert found.name == STOP_NAME and found.map_ref_id == "s1"
    assert found.address == "Xã TEST, Tỉnh Ninh Bình"
    assert found.lat == STOP[0] and found.power_kw is None and found.connector == "unknown"
    assert not found.simulated and not result.incomplete
    assert maps.details == ["s1"]
    assert len(maps.queries) <= 48
    assert all(kwargs["layers"] == "POI" and kwargs["radius_m"] == 20000 for _, kwargs in maps.queries)


@pytest.mark.parametrize(
    "metadata,has_power",
    [
        ([equipment()], True),
        ([equipment(simulated=True)], False),
        ([equipment(map_ref_id="another-site")], False),
        ([equipment(lat=21)], False),
        ([equipment(), equipment(id="duplicate")], False),
    ],
)
@pytest.mark.asyncio
async def test_power_requires_unambiguous_non_synthetic_equipment_evidence(metadata, has_power):
    result = await VietMapChargingDiscovery(Maps(), metadata).discover(road().model_dump())
    assert bool(result.stations[0].power_kw) == has_power
    assert result.stations[0].name == STOP_NAME and result.stations[0].map_ref_id == "s1"


@pytest.mark.asyncio
async def test_map_failure_never_substitutes_catalogue_or_synthetic_coordinates():
    result = await VietMapChargingDiscovery(Maps(fail=True), [equipment()]).discover(road().model_dump())
    assert not result.stations and result.incomplete and "station_search_failed" in result.errors


@pytest.mark.asyncio
async def test_discovery_timeout_is_typed_and_bounded():
    class SlowMaps(Maps):
        async def search(self, *args, **kwargs):
            await asyncio.sleep(1)

    result = await VietMapChargingDiscovery(SlowMaps(), budget_seconds=0.01).discover(road().model_dump())
    assert not result.stations and result.incomplete and result.errors == ["station_search_timeout"]


@pytest.mark.parametrize("known,status", [(True, "planned"), (False, "station_data_required")])
@pytest.mark.asyncio
async def test_hanoi_thanhhoa_uses_road_legs_and_discovered_pois(services, known, status):
    maps = Maps(opposite=True)
    services = replace(
        services,
        navigation=maps,
        energy_planner=EnergyPlanner(profile()),
        charging_discovery=VietMapChargingDiscovery(maps, [equipment()] if known else []),
    )
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]},
        route=road().model_dump(),
    )
    result, plans = await NavigationWorkflow(services).plan_energy(nav, 50, "rules")
    assert result["status"] == status and result["station_search"]["source"] == "vietmap"
    assert len(result["station_candidates"]) == 1  # wrong carriageway adds 70km
    candidate = result["station_candidates"][0]
    assert candidate["distance_m"] == 70000 and candidate["arrival_soc"] == pytest.approx(26.67, abs=0.01)
    assert candidate["road_context"] == "Cao tốc TEST" and candidate["detour_m"] == 0
    if known:
        plan = plans[0]
        assert plan["arrival_soc"] == 15 and len(plan["stops"]) == 1
        stop = plan["stops"][0]
        assert stop["map_ref_id"] == "s1" and stop["connector"] == "CCS2" and not stop["simulated"]
        assert stop["charge_minutes"] == pytest.approx(14)
    else:
        assert not plans and candidate["power_kw"] is None and "charge_minutes" not in candidate


@pytest.mark.parametrize("case", ["no_road", "low_soc", "incompatible_connector"])
@pytest.mark.asyncio
async def test_unusable_stations_are_not_proposed(services, case):
    maps = Maps(missing_leg=case == "no_road")
    metadata = [equipment(connector="CHAdeMO")] if case == "incompatible_connector" else []
    services = replace(
        services,
        navigation=maps,
        energy_planner=EnergyPlanner(profile()),
        charging_discovery=VietMapChargingDiscovery(maps, metadata),
    )
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]},
        route=road().model_dump(),
    )
    result, plans = await NavigationWorkflow(services).plan_energy(nav, 30 if case == "low_soc" else 50, "rules")
    assert (
        result["status"] == ("incomplete" if case == "no_road" else "no_feasible_plan")
        and not result["station_candidates"]
        and not plans
    )


@pytest.mark.asyncio
async def test_sufficient_battery_needs_no_station_lookup(services):
    maps = Maps(fail=True)
    services = replace(
        services,
        navigation=maps,
        energy_planner=EnergyPlanner(profile()),
        charging_discovery=VietMapChargingDiscovery(maps),
    )
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]},
        route=road().model_dump(),
    )
    result, plans = await NavigationWorkflow(services).plan_energy(nav, 90, "rules")
    assert result["status"] == "sufficient" and plans[0]["stops"] == [] and maps.queries == []


@pytest.mark.asyncio
async def test_old_synthetic_pending_itinerary_cannot_start_with_live_discovery(services):
    maps = Maps()
    services = replace(
        services,
        navigation=maps,
        energy_planner=EnergyPlanner(profile()),
        charging_discovery=VietMapChargingDiscovery(maps),
    )
    nav = NavigationState(energy={"selected_plan": {"stops": [{"name": "Old synthetic stop", "simulated": True}]}})
    result = await NavigationWorkflow(services).start(SimpleNamespace(session_id="driver"), nav, "old-command", {})
    assert result["status"] == "blocked" and result["error_code"] == "station_source_changed"
    assert not services.action_gateway.vehicle.state_for("driver").driving


@pytest.mark.asyncio
async def test_missing_power_user_can_select_real_station_by_voice_then_confirm(services, tmp_path):
    maps = Maps()
    services = replace(
        services,
        navigation=maps,
        energy_planner=EnergyPlanner(profile()),
        charging_discovery=VietMapChargingDiscovery(maps),
    )
    services.action_gateway.vehicle.set_energy("driver", 50, 150)
    graph, _, vehicle = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "plan",
                "input_text": "chỉ đường tới điểm đến TEST Thanh Hóa",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1], "simulated": False},
            }
        )
        blocked = await invoke(graph, "chỗ đầu tiên", "choose-destination")
        assert blocked["status"] == "blocked" and not vehicle.vehicle.state_for("driver").driving
        assert "Trạm Dừng Nghỉ TEST" in blocked["response_text"] and "70 km" in blocked["response_text"]
        assert "mô phỏng" not in blocked["response_text"] and not blocked.get("confirmation")
        selected = await invoke(graph, "đến trạm sạc đầu tiên", "choose-charger")
        assert selected["navigation"]["selected"]["ref_id"] == "s1"
        assert selected["status"] == "answered" and not vehicle.vehicle.state_for("driver").driving
        assert selected["navigation"]["offer"]["status"] == "pending"
        running = await invoke(graph, "đồng ý", "start")
        assert running["navigation"]["status"] == "running" and running["navigation"]["selected"]["ref_id"] == "s1"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_confirmed_map_charging_stop_survives_restart_without_catalogue_lookup(services, tmp_path):
    maps = Maps()
    services = replace(
        services,
        navigation=maps,
        energy_planner=EnergyPlanner(profile()),
        charging_discovery=VietMapChargingDiscovery(maps, [equipment()]),
    )
    services.action_gateway.vehicle.set_energy("driver", 50, 150)
    graph, ledger, _ = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "plan",
                "input_text": "chỉ đường tới điểm đến TEST Thanh Hóa",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1], "simulated": False},
            }
        )
        preview = await invoke(graph, "chỗ đầu tiên", "choose")
        assert preview["status"] == "answered" and preview["navigation"]["offer"]["status"] == "pending"
        assert preview["navigation"]["summary"]["stops"][0]["road_context"] == "Cao tốc TEST"
        running = await invoke(graph, "đồng ý", "start")
        stopped = await progress(graph, running["navigation"], "at-station", kind="progress", distance_m=70000)
        graph.close()
        before = len(maps.queries)
        graph, _, _ = build(services, tmp_path, store=ledger)
        nav = stopped["navigation"]
        # A restart parks the journey; the driver resumes it before charging goes on.
        await progress(graph, nav, "resume-charge", kind="resume")
        for index in range(12):
            nav = (await progress(graph, nav, f"charge-{index}", kind="charge", elapsed_s=30))["navigation"]
        assert nav["simulated_soc"] > 26.67 and len(maps.queries) == before
        replay = await progress(graph, nav, "charge-11", kind="charge", elapsed_s=30)
        assert replay["navigation"] == nav
    finally:
        graph.close()
