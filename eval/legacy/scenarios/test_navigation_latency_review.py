"""Regressions for partial station searches and replans after GPS/SOC changes."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.dialogue.contracts import NavigationState, SessionState
from src.vivi.navigation.charging import VietMapChargingDiscovery, reproject_stations
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.workflow import NavigationWorkflow
from tests.support.charging_discovery import DEST, ORIGIN, STOP, Maps, equipment, road
from tests.support.long_term_memory import services as services
from tests.support.navigation_lifecycle import runtime
from tests.support.navigation_workflow import profile


@pytest.mark.asyncio
async def test_slow_search_keeps_successful_stations_and_drains_pending_requests():
    class PartialMaps(Maps):
        pending = 0

        async def search(self, text, **kwargs):
            self.pending += 1
            try:
                if text == "trạm dừng nghỉ":
                    await asyncio.sleep(10)
                return await super().search(text, **kwargs)
            finally:
                self.pending -= 1

    maps = PartialMaps(detail=True)
    result = await VietMapChargingDiscovery(maps, [equipment()], budget_seconds=0.3).discover(road().model_dump())
    assert result.incomplete and "station_search_timeout" in result.errors
    assert [s.map_ref_id for s in result.stations] == ["s1"]
    assert result.stations[0].power_kw == 100
    assert maps.pending == 0


@pytest.mark.asyncio
async def test_workflow_preserves_partial_discovery_instead_of_outer_timeout(services):
    class PartialMaps(Maps):
        async def search(self, text, **kwargs):
            if text == "trạm dừng nghỉ":
                await asyncio.sleep(10)
            return await super().search(text, **kwargs)

    maps = PartialMaps()
    discovery = VietMapChargingDiscovery(maps, [equipment()], budget_seconds=0.3)
    workflow = NavigationWorkflow(replace(
        services, navigation=maps, energy_planner=EnergyPlanner(profile()), charging_discovery=discovery,
    ))
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]}, route=road().model_dump(),
    )
    energy, plans = await workflow.plan_energy(nav, 50, "rules")
    assert energy["status"] == "planned" and plans
    assert energy["station_search"]["errors"] == ["station_search_timeout"]
    assert energy["corridor_stations"][0]["map_ref_id"] == "s1"


@pytest.mark.asyncio
async def test_soc_replan_reuses_recent_pois_but_rechecks_road_legs(services):
    maps = Maps()
    discovery = VietMapChargingDiscovery(maps, [equipment()])
    workflow = NavigationWorkflow(replace(
        services, navigation=maps, energy_planner=EnergyPlanner(profile()), charging_discovery=discovery,
    ))
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]}, route=road().model_dump(),
    )
    nav.energy, nav.alternatives = await workflow.plan_energy(nav, 50, "rules")
    assert nav.energy["status"] == "planned"
    count = len(maps.queries)
    maps.roads.clear()
    maps.fail = True  # Search is unavailable when the driver accepts the offer.
    revised, plans = await workflow.plan_energy(nav, 49, "rules")
    assert revised["status"] == "planned" and revised["station_search"]["cache_hit"]
    assert len(maps.queries) == count
    assert (ORIGIN, STOP) in maps.roads and (STOP, DEST) in maps.roads
    assert plans[0]["stops"][0]["arrival_soc"] < nav.alternatives[0]["stops"][0]["arrival_soc"]
    assert plans[0]["arrival_soc"] >= workflow.energy.reserve
    assert revised["station_search"]["observed_at"] == nav.energy["station_search"]["observed_at"]

    maps.missing_leg = True
    failed, plans = await workflow.plan_energy(nav, 49, "rules")
    assert not plans and failed["status"] != "planned"
    assert len(maps.queries) > count  # A cache that cannot produce a plan refreshes once.


@pytest.mark.parametrize("change", ["expired", "future", "profile", "equipment", "destination", "malformed"])
@pytest.mark.asyncio
async def test_cache_does_not_reuse_stale_or_changed_calculation_inputs(services, change):
    maps = Maps()
    discovery = VietMapChargingDiscovery(maps, [equipment()])
    workflow = NavigationWorkflow(replace(
        services, navigation=maps, energy_planner=EnergyPlanner(profile()), charging_discovery=discovery,
    ))
    nav = NavigationState(
        origin={"lat": ORIGIN[0], "lng": ORIGIN[1]},
        selected={"lat": DEST[0], "lng": DEST[1]}, route=road().model_dump(),
    )
    nav.energy, nav.alternatives = await workflow.plan_energy(nav, 50, "rules")
    if change in {"expired", "future"}:
        delta = timedelta(minutes=-11 if change == "expired" else 1)
        nav.energy["station_search"]["observed_at"] = (datetime.now(UTC) + delta).isoformat()
    elif change == "profile":
        workflow.energy.profile = profile().model_copy(update={"consumption_kwh_per_100km": 25})
    elif change == "equipment":
        discovery.equipment = [equipment(power_kw=50)]
    elif change == "destination":
        nav.selected["lat"] += 0.1
    else:
        nav.energy["corridor_stations"] = [{"id": "broken"}]
    assert await workflow.cached_discovery(nav, workflow.station_basis(nav)) is None


def test_reused_pois_get_new_progress_and_off_corridor_or_synthetic_stations_are_removed():
    station = equipment()
    shifted = road((20.7, 105.85), DEST, 120000).model_dump()
    projected = reproject_stations([
        station, station.model_copy(update={"simulated": True}),
        station.model_copy(update={"lng": 109}), station.model_copy(update={"map_ref_id": None}),
    ], shifted, 20000)
    assert len(projected) == 1 and 0 < projected[0].route_progress_m < 120000
    assert station.route_progress_m is None


@pytest.mark.asyncio
async def test_hydration_isolates_domain_cache_and_preserves_original_session(services, tmp_path):
    graph, _, _ = runtime(services, tmp_path)
    try:
        session = SessionState()
        session.navigation_workspace.origin = {"lat": ORIGIN[0], "lng": ORIGIN[1]}
        session.navigation_workspace.draft = NavigationState(
            route=road().model_dump(), energy={"station_candidates": [{"id": "original"}]},
        )
        await graph.navigation.save(session, "driver")
        before = deepcopy(session.model_dump(mode="json"))
        hydrated = await graph.navigation.hydrate(session, "driver")
        hydrated.navigation_workspace.draft.route["coordinates"][0][0] = 0
        hydrated.navigation_workspace.draft.energy["station_candidates"][0]["id"] = "changed"
        assert session.model_dump(mode="json") == before
        again = await graph.navigation.hydrate(session, "driver")
        assert again.navigation_workspace.draft.route["coordinates"][0][0] == ORIGIN[1]
        assert again.navigation_workspace.draft.energy["station_candidates"][0]["id"] == "original"
    finally:
        await graph.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_latency_log_matches_client_without_adding_overlapping_spans(tmp_path, stream):
    from src.vivi.config import Settings
    from src.vivi.domain.models import TurnRequest
    from src.vivi.orchestration import LangGraphOrchestrator
    from src.vivi.persistence.event_store import EventStore

    class Graph:
        async def ainvoke(self, state):
            return {"output": {
                "session_id": state["session_id"], "turn_id": state["turn_id"],
                "route": "conversation", "intent": "conversation.respond", "status": "answered",
                "response_text": "Đã tính xong.", "tts_text": "Đã tính xong.",
                "timings": {"capability": 8000, "navigation_charging_discovery": 8000},
            }}

        async def astream(self, state, stream_mode):
            yield "values", await self.ainvoke(state)

    store = EventStore(Settings(data_dir=tmp_path))
    pipeline = LangGraphOrchestrator({"rules": Graph()}, None, store, default_provider="rules")
    request = TurnRequest(session_id="latency", turn_id="one", transcript="Tính tuyến")
    if stream:
        events = [item async for item in pipeline.run_stream(request)]
        latency = events[-1]["response"]["latency_ms"]
    else:
        latency = (await pipeline.run(request)).latency_ms
    assert latency["total"] < 16000  # The fake graph has no dependency wait.
    assert latency["navigation_charging_discovery"] == 8000
    assert store.last_event["latency_ms"] == latency
