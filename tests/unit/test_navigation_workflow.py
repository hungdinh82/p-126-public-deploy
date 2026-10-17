"""Episodes from the latest audit and route/energy invariants with no network."""

from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn, SessionState
from src.vivi.dialogue.reducer import record_step, reduce_turn
from src.vivi.navigation.energy import EnergyPlanner
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import HAIPHONG as HAIPHONG
from tests.support.navigation_workflow import HANOI as HANOI
from tests.support.navigation_workflow import STOP as STOP
from tests.support.navigation_workflow import IntercityMaps as IntercityMaps
from tests.support.navigation_workflow import Maps as Maps
from tests.support.navigation_workflow import profile as profile
from tests.support.navigation_workflow import progress as progress
from tests.support.navigation_workflow import station as station


def test_navigation_event_request_validation():
    from pydantic import ValidationError

    from src.vivi.domain.models import TurnRequest

    for event in [
        {"kind": "point", "id": "e", "revision": 0},
        {"kind": "progress", "id": "e", "revision": 0, "distance_m": -1},
        {"kind": "charge", "id": "e", "revision": 0, "elapsed_s": 31},
    ]:
        with pytest.raises(ValidationError):
            TurnRequest(session_id="s", turn_id="t", transcript="event", navigation_event=event)


def test_memory_repair_backup_and_compare_and_swap(tmp_path):
    import sqlite3

    from src.vivi.persistence.migration import repair_logged_name

    path = tmp_path / "memory.sqlite3"
    turn = "task:e69a22f9-c96c-4878-88bb-3eae50d7b98b:r2:step:3030dede-be42-47ca-991b-53a344fce3e9"
    with sqlite3.connect(path) as db:
        db.execute("create table memory_items (profile_id,key,value_json,source_turn_id,revision)")
        db.execute("insert into memory_items values (?,?,?,?,?)", ("default", "display_name", '"tôi"', turn, 2))
    assert not repair_logged_name(path)  # user corrected revision wins
    with sqlite3.connect(path) as db:
        db.execute("update memory_items set revision=1")
    assert repair_logged_name(path)
    backup = list(tmp_path.glob("*before-name-repair*.sqlite3"))
    assert len(backup) == 1
    with sqlite3.connect(backup[0]) as db:
        assert db.execute("select value_json from memory_items").fetchone()[0] == '"tôi"'
    assert not repair_logged_name(path)


def route(distance):
    return {"distance_m": distance, "duration_s": distance / 20, "coordinates": [[105, 21], [106, 21]], "steps": []}


def test_charging_math_and_station_limit():
    planner = EnergyPlanner(profile())
    assert planner.charging_minutes(station(), 20, 70) == pytest.approx(23)  # 30kWh/100kW*60 +5
    assert planner.charging_minutes(station(), 70, 90) == pytest.approx(17.6)
    with pytest.raises(ValueError):
        planner.charging_minutes(station(power=250), 20, 90)


def test_route_solver_one_and_multiple_charging_stops():
    planner = EnergyPlanner(profile())
    stops = [station("s1"), station("s2")]
    edges = {
        ("origin", "destination"): route(450000),
        ("origin", "s1"): route(90000),
        ("s1", "s2"): route(180000),
        ("s2", "destination"): route(180000),
    }
    plans = planner.solve(None, None, 50, edges, stops)
    assert plans and len(plans[0]["stops"]) == 2
    assert all(leg["arrival_soc"] >= 15 for leg in plans[0]["legs"])
    assert planner.solve(None, None, 10, edges, stops) == []


def test_enough_energy_no_stops_and_missing_data():
    planner = EnergyPlanner(profile())
    plans = planner.solve(None, None, 80, {("origin", "destination"): route(100000)}, [])
    assert plans[0]["stops"] == [] and plans[0]["arrival_soc"] == pytest.approx(46.67, abs=0.01)
    assert EnergyPlanner().assess(route(100000), 80)["status"] == "configuration_required"
    assert not EnergyPlanner(profile(), [station(power=None)]).compatible_stations()


def test_expired_grant_retains_task_and_reissues_permission():
    state, _ = reduce_turn(
        SessionState(),
        SemanticTurn(
            act="request", actions=[ActionDraft(capability="door.set_open", arguments={"zone": "driver", "open": True})]
        ),
    )
    original = state.active.expires_at
    state = record_step(
        state,
        0,
        status="waiting_confirmation",
        confirmation={"confirmation_id": "old", "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    assert state.active.expires_at == original
    state, message = reduce_turn(state, SemanticTurn(act="confirm", task_ref="active"))
    assert state.active.steps[0].status == "pending" and not state.active.steps[0].confirmation
    assert message is None
