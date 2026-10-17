"""Focused log 974/984/986–991 replay; all map routes are labelled fixtures."""

import pytest

from src.vivi.navigation.energy import EnergyPlanner
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile, station
from tests.support.trip_plans import CHARGER as CHARGER
from tests.support.trip_plans import DEST as DEST
from tests.support.trip_plans import ORIGIN as ORIGIN
from tests.support.trip_plans import TripMaps as TripMaps
from tests.support.trip_plans import runtime as runtime


def test_round_trip_solver_carries_soc_and_uses_actual_return():
    planner = EnergyPlanner(profile())
    maps = {}
    for a, b, d in [
        ("origin", "destination", 120000),
        ("destination", "return", 150000),
        ("origin", "s", 60000),
        ("s", "destination", 60000),
        ("destination", "s", 80000),
        ("s", "return", 70000),
    ]:
        maps[a, b] = {"distance_m": d, "duration_s": d / 20, "coordinates": [[105, 21], [105, 20]], "steps": []}
    outward = planner.solve(ORIGIN, DEST, 65, maps, [])
    assert outward and not outward[0]["stops"]
    assert not planner.solve(ORIGIN, DEST, 65, maps, [], waypoints=["destination", "return"])
    plans = planner.solve(
        ORIGIN, DEST, 65, maps, [station().model_copy(update={"id": "s"})], waypoints=["destination", "return"]
    )
    assert plans and plans[0]["stops"]
    plan = plans[0]
    legs = plan["legs"]
    assert any(leg["to"] == "destination" for leg in legs) and legs[-1]["to"] == "return"
    for before, after in zip(legs, legs[1:]):
        if before["to"] == "destination":
            assert before["arrival_soc"] == after["departure_soc"]
    assert plan["arrival_soc"] >= 15 and plan["distance_m"] == 270000


def test_same_station_can_have_two_independent_visits():
    planner = EnergyPlanner(profile())
    # Each direction needs a charge. Passing the destination never adds SOC.
    edges = {}
    for a, b, d in [
        ("origin", "destination", 220000),
        ("destination", "return", 240000),
        ("origin", "s", 110000),
        ("s", "destination", 110000),
        ("destination", "s", 120000),
        ("s", "return", 120000),
    ]:
        edges[a, b] = {"distance_m": d, "duration_s": d / 20, "coordinates": [[105, 21], [105, 20]], "steps": []}
    plans = planner.solve(
        ORIGIN, DEST, 60, edges, [station().model_copy(update={"id": "s"})], waypoints=["destination", "return"]
    )
    assert plans
    stops = plans[0]["stops"]
    assert len(stops) == 2 and [s["station_id"] for s in stops] == ["s", "s"]
    assert stops[0]["visit_id"] != stops[1]["visit_id"] and stops[0]["leg_index"] < stops[1]["leg_index"]
    between = plans[0]["legs"][stops[0]["leg_index"] + 1 : stops[1]["leg_index"] + 1]
    assert stops[1]["arrival_soc"] == pytest.approx(
        stops[0]["departure_soc"] - planner.soc_cost(sum(leg["distance_m"] for leg in between)), abs=0.01
    )
    assert stops[0]["departure_soc"] != stops[1]["departure_soc"]
