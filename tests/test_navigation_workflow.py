"""Episodes from the latest audit and route/energy invariants with no network."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn, SessionState
from src.vivi.dialogue.reducer import record_step, reduce_turn
from src.vivi.navigation.energy import ChargeBand, ChargingStation, EnergyPlanner, EnergyProfile
from src.vivi.navigation.vietmap import Place, Route, VietMapError
from tests.test_dialogue_v2 import ScriptedModel, build, invoke
from tests.test_long_term_memory import services as services

HANOI = (21.0287, 105.8524)
HAIPHONG = (20.8449, 106.6881)
STOP = (21.0, 106.2)


class IntercityMaps:
    """Synthetic road lengths; coordinates identify endpoints, not real stations."""

    async def search(self, text, **kwargs):
        return [
            Place(
                ref_id="haiphong-test",
                name="Hải Phòng TEST",
                address="Điểm đại diện mô phỏng",
                lat=HAIPHONG[0],
                lng=HAIPHONG[1],
            )
        ]

    async def route(self, a, b):
        distances = {(HANOI, HAIPHONG): 120000, (HANOI, STOP): 50000, (STOP, HAIPHONG): 70000}
        distance = distances.get((a, b))
        if distance is None:
            raise VietMapError("No verified road fixture")
        return Route(distance_m=distance, duration_s=distance / 20, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


async def progress(graph, nav, identifier, **values):
    return await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": identifier,
            "input_text": "Cập nhật mô phỏng",
            "navigation_event": {"id": identifier, "revision": nav["revision"], **values},
        }
    )


@pytest.mark.parametrize("soc,stops", [(70, 0), (40, 1)])
@pytest.mark.asyncio
async def test_hanoi_haiphong_energy_and_simulation_checkpoint(services, tmp_path, soc, stops):
    vehicle = services.action_gateway.vehicle
    vehicle.set_energy("driver", soc, soc * 3)
    services = replace(
        services,
        navigation=IntercityMaps(),
        energy_planner=EnergyPlanner(profile(), [station(lat=STOP[0], lng=STOP[1])]),
    )
    graph, store, _ = build(services, tmp_path)
    result = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "plan",
            "input_text": "chỉ đường tới Hải Phòng TEST",
            "position": {"lat": HANOI[0], "lng": HANOI[1], "simulated": True},
        }
    )
    assert result["navigation"]["status"] == "selecting" and not (await vehicle.get_state("driver")).driving
    result = await invoke(graph, "chỗ đầu tiên", "select")
    plan = result["navigation"]["energy"]["selected_plan"]
    assert len(plan["stops"]) == stops and plan["arrival_soc"] >= 15
    assert plan["simulated"] and result["status"] == "answered"
    if stops:
        assert plan["stops"][0]["arrival_soc"] == pytest.approx(23.33, abs=0.01)
        assert plan["stops"][0]["charge_minutes"] == pytest.approx(10.4)
    result = await invoke(graph, "đồng ý", "start")
    nav = result["navigation"]
    if stops:
        result = await progress(graph, nav, "to-stop", kind="progress", distance_m=60000)
        nav = result["navigation"]
        assert nav["status"] == "charging" and nav["progress_m"] == 50000
        replay = await progress(graph, nav, "to-stop", kind="progress", distance_m=60000)
        assert replay["navigation"] == nav
        for index in range(12):
            result = await progress(graph, nav, f"charge-{index}", kind="charge", elapsed_s=30)
            nav = result["navigation"]
        assert 23.33 < nav["simulated_soc"] < 38.34
        graph.close()
        graph, _, _ = build(services, tmp_path, store=store)
        restored = await graph.snapshot("driver")
        assert restored["navigation"]["status"] == "paused"
        await progress(graph, nav, "resume-charge", kind="resume")
        for index in range(12, 21):
            result = await progress(graph, nav, f"charge-{index}", kind="charge", elapsed_s=30)
            nav = result["navigation"]
        assert nav["status"] == "running" and nav["stop_index"] == 1
    result = await progress(graph, nav, "finish", kind="progress", distance_m=120000)
    assert result["navigation"]["status"] == "completed"
    assert result["navigation"]["simulated_soc"] == pytest.approx(plan["arrival_soc"], abs=0.02)
    assert result["vehicle_state"]["battery_percent"] == int(result["navigation"]["simulated_soc"])
    assert result["diagnostics"]["observation_available"]
    assert not (await vehicle.get_state("driver")).driving
    graph.close()


@pytest.mark.asyncio
async def test_pending_destination_keeps_running_route_until_confirmation(services, tmp_path):
    services = replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    graph, _, _ = build(services, tmp_path)
    await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "plan",
            "input_text": "dẫn tôi đến hiệu thuốc gần nhất",
            "position": {"lat": 21, "lng": 105.8, "simulated": True},
        }
    )
    selected = await invoke(graph, "chỗ đầu tiên", "choose")
    assert selected["status"] == "answered"
    started = await invoke(graph, "đồng ý", "start")
    before = started["navigation"]
    result = await progress(
        graph, before, "point", kind="point", position={"lat": 21, "lng": 106, "label": "Điểm TEST", "simulated": True}
    )
    nav = result["navigation"]
    assert nav["status"] == "running" and nav["route"] == before["route"]
    assert nav["pending"], result
    assert nav["pending"]["navigation"]["selected"]["name"] == "Điểm TEST"
    assert result["status"] == "answered"
    stale = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "stale-point",
            "navigation_event": {
                "id": "stale-point",
                "revision": before["revision"],
                "kind": "point",
                "position": {"lat": 21, "lng": 107, "label": "Điểm cũ"},
            },
        }
    )
    assert stale["navigation"] == nav
    assert stale["session"]["active"]["steps"][0]["confirmation"] == result["confirmation"]
    cancelled = await invoke(graph, "không", "cancel")
    assert cancelled["navigation"]["status"] == "running"
    assert cancelled["navigation"]["pending"]["navigation"]["offer"]["status"] == "declined"
    result = await progress(
        graph,
        cancelled["navigation"],
        "point-again",
        kind="point",
        position={"lat": 21, "lng": 106, "label": "Điểm TEST", "simulated": True},
    )
    result = await invoke(graph, "đồng ý", "change")
    assert result["navigation"]["selected"]["name"] == "Điểm TEST" and result["navigation"]["status"] == "running"
    assert not result["navigation"]["pending"]
    graph.close()


@pytest.mark.asyncio
async def test_select_energy_alternative_reissues_grant_before_route_change(services, tmp_path):
    services = replace(
        services,
        navigation=IntercityMaps(),
        energy_planner=EnergyPlanner(profile(), [station(lat=STOP[0], lng=STOP[1])]),
    )
    services.action_gateway.vehicle.set_energy("driver", 40, 120)
    graph, _, _ = build(services, tmp_path)
    await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "plan",
            "input_text": "chỉ đường tới Hải Phòng TEST",
            "position": {"lat": HANOI[0], "lng": HANOI[1], "simulated": True},
        }
    )
    result = await invoke(graph, "chỗ đầu tiên", "select")
    nav = result["navigation"]
    alternative = next(p for p in nav["alternatives"] if p["strategy"] == "higher_reserve")
    selected = await progress(graph, nav, "alternative", kind="select_plan", plan_id=alternative["id"])
    assert (
        selected["status"] == "answered"
        and selected["confirmation"]["confirmation_id"] != result["confirmation"]["confirmation_id"]
    )
    assert not (await services.action_gateway.vehicle.get_state("driver")).driving
    started = await invoke(graph, "đồng ý", "start")
    assert started["navigation"]["energy"]["selected_plan"]["id"] == alternative["id"]
    assert started["navigation"]["status"] == "running"
    pending = await progress(
        graph,
        started["navigation"],
        "new-preview",
        kind="point",
        position={"lat": HAIPHONG[0], "lng": HAIPHONG[1], "label": "Hải Phòng TEST", "simulated": True},
    )
    candidate = pending["navigation"]["pending"]["navigation"]
    other = next(p for p in candidate["alternatives"] if p["strategy"] == "fewest_stops")
    revised = await progress(
        graph, pending["navigation"], "pending-alternative", kind="select_plan", plan_id=other["id"]
    )
    assert revised["navigation"]["status"] == "running" and revised["status"] == "answered"
    assert revised["navigation"]["energy"]["selected_plan"]["id"] == alternative["id"]
    accepted = await invoke(graph, "đồng ý", "approve-new-plan")
    assert accepted["navigation"]["energy"]["selected_plan"]["id"] == other["id"]
    assert not accepted["navigation"]["pending"]
    graph.close()


@pytest.mark.asyncio
async def test_route_forecast_alert_telemetry_hysteresis_and_severity(services, tmp_path):
    vehicle = services.action_gateway.vehicle
    services = replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    graph, _, _ = build(services, tmp_path)
    await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "plan",
            "input_text": "dẫn tôi đến hiệu thuốc gần nhất",
            "position": {"lat": 21, "lng": 105.8, "simulated": True},
        }
    )
    await invoke(graph, "chỗ đầu tiên", "choose")
    nav = (await invoke(graph, "đồng ý", "start"))["navigation"]
    vehicle.set_energy("driver", 40, 120)
    nav = (await progress(graph, nav, "low", kind="progress", distance_m=1000))["navigation"]
    assert nav["alerts"] and nav["alerts"][0]["severity"] == "warning"
    alert_id = nav["alerts"][0]["alert_id"]
    assert nav["pending"]["status"] == "no_feasible_plan"
    nav = (await progress(graph, nav, "still-low", kind="progress", distance_m=2000))["navigation"]
    assert nav["alerts"][0]["alert_id"] == alert_id
    vehicle.set_energy("driver", 8, 24)
    nav = (await progress(graph, nav, "critical", kind="progress", distance_m=3000))["navigation"]
    assert nav["alerts"][0]["severity"] == "critical" and nav["alerts"][0]["alert_id"] != alert_id
    assert nav["status"] == "running"  # alert never silently changes the route
    graph.close()


@pytest.mark.asyncio
async def test_simulator_process_restart_restores_checkpoint_energy_without_replaying_start(services, tmp_path):
    from src.vivi.vehicle.gateway import VehicleActionGateway

    services = replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    graph, ledger, _ = build(services, tmp_path)
    await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "plan",
            "input_text": "dẫn tôi đến hiệu thuốc gần nhất",
            "position": {"lat": 21, "lng": 105.8, "simulated": True},
        }
    )
    await invoke(graph, "chỗ đầu tiên", "choose")
    nav = (await invoke(graph, "đồng ý", "start"))["navigation"]
    before = await progress(graph, nav, "before-restart", kind="progress", distance_m=5000)
    graph.close()
    restarted = replace(services, action_gateway=VehicleActionGateway())
    assert not (await restarted.action_gateway.vehicle.get_state("driver")).driving
    graph, _, _ = build(restarted, tmp_path, store=ledger)
    after = await progress(graph, before["navigation"], "after-restart", kind="progress", distance_m=6000)
    assert after["navigation"]["status"] == "paused"
    assert after["navigation"]["simulated_soc"] == before["navigation"]["simulated_soc"]
    assert after["navigation"]["progress_m"] == 5000
    assert not (await restarted.action_gateway.vehicle.get_state("driver")).driving
    resumed = await progress(graph, after["navigation"], "resume-after-restart", kind="resume")
    assert resumed["navigation"]["status"] == "running"
    graph.close()


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


@pytest.mark.asyncio
async def test_voice_navigation_through_http_stream_confirmation_and_progress(services, tmp_path, client, monkeypatch):
    from src.vivi.api.runtime import runtime
    from src.vivi.config import Settings
    from src.vivi.orchestration import LangGraphOrchestrator
    from src.vivi.persistence.event_store import EventStore

    services = replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    graph, ledger, gateway = build(services, tmp_path)
    store = EventStore(Settings(data_dir=tmp_path / "api", store_transcripts=False, store_audio=False))
    orchestrator = LangGraphOrchestrator({"rules": graph}, gateway, store, ledger=ledger)
    monkeypatch.setattr(runtime, "orchestrator", orchestrator)
    monkeypatch.setattr(runtime, "store", store)
    response = await client.post(
        "/api/v1/turn/stream",
        json={
            "session_id": "driver",
            "turn_id": "api-plan",
            "transcript": "dẫn tôi đến hiệu thuốc gần nhất",
            "position": {"lat": 21, "lng": 105.8, "simulated": True},
            "llm_provider": "rules",
        },
    )
    assert response.status_code == 200
    import json

    events = [json.loads(line) for line in response.text.splitlines()]
    assert any(event["type"] == "navigation" and event["navigation"]["status"] == "selecting" for event in events)
    choice = await client.post("/api/v1/turn", json={"session_id": "driver", "turn_id": "api-choice", "transcript": "chỗ đầu tiên", "llm_provider": "rules"})
    assert choice.status_code == 200
    preview = choice.json()
    assert (
        preview["status"] == "verified"
        and not (await services.action_gateway.vehicle.get_state("driver")).driving
    )
    approved = await client.post(
        "/api/v1/confirmations/" + preview["confirmation"]["confirmation_id"],
        json={"session_id": "driver", "turn_id": "api-approve", "decision": "approve", "llm_provider": "rules"},
    )
    result = approved.json()
    assert result["status"] == "verified" and result["navigation"]["status"] == "running"
    assert result["execution"]["executed"] and result["execution"]["verified"]
    update = await client.post(
        "/api/v1/turn",
        json={
            "session_id": "driver",
            "turn_id": "api-progress",
            "transcript": "Cập nhật",
            "navigation_event": {
                "kind": "progress",
                "id": "p1",
                "revision": result["navigation"]["revision"],
                "distance_m": 1000,
            },
            "llm_provider": "rules",
        },
    )
    assert update.status_code == 200
    assert update.json()["vehicle_state"]["battery_percent"] == int(update.json()["navigation"]["simulated_soc"])
    invalid = await client.post(
        "/api/v1/turn",
        json={
            "session_id": "driver",
            "turn_id": "invalid",
            "transcript": "Cập nhật",
            "navigation_event": {"kind": "point", "id": "p2", "revision": result["navigation"]["revision"]},
        },
    )
    assert invalid.status_code == 422
    graph.close()


def profile():
    return EnergyProfile(
        id="fixture-ev",
        vehicle_model="VF8",
        model_year=2026,
        edition="TEST ONLY",
        usable_kwh=60,
        consumption_kwh_per_100km=20,
        max_charge_kw=100,
        conservative_factor=1,
        charge_curve=[ChargeBand(until_soc=80, power_kw=100), ChargeBand(until_soc=100, power_kw=40)],
        source="Synthetic hand-calculated test fixture",
        updated_at="2026-10-09",
        simulated=True,
    )


def station(identifier="s1", lat=21, lng=106, power=100):
    return ChargingStation(
        id=identifier,
        name="Trạm sạc VinFast TEST " + identifier,
        lat=lat,
        lng=lng,
        network="vgreen",
        connector="CCS2",
        power_kw=power,
        source="Synthetic test fixture",
        updated_at="2026-10-09",
        simulated=True,
    )


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


@pytest.mark.asyncio
async def test_valid_unsupported_fragment_never_becomes_clarification_or_fallback(services, tmp_path):
    model = ScriptedModel(
        [{"act": "unsupported", "text": "Mình chưa hỗ trợ chỉnh quạt gió.", "unresolved": ["Tăng quạt gió"]}]
    )
    graph, _, _ = build(services, tmp_path, model)
    result = await invoke(graph, "Tăng quạt gió", "unsupported")
    assert result["status"] == "unsupported" and result["diagnostics"]["effective_provider"] == "google"
    assert not result.get("execution")
    graph.close()


@pytest.mark.asyncio
async def test_model_omitted_clause_cannot_execute_and_draft_survives(services, tmp_path):
    model = ScriptedModel([{"act": "request", "actions": [{"capability": "media.play"}]}])
    graph, _, _ = build(services, tmp_path, model)
    result = await invoke(graph, "bật nhạc và gọi điện cho An", "partial")
    assert (
        result["status"] == "clarify" and not (await services.action_gateway.vehicle.get_state("driver")).media_playing
    )
    assert result["diagnostics"]["effective_provider"] == "google"
    graph.default_provider = "rules"
    result = await invoke(graph, "đổi thành đặt nhiệt độ 25 độ", "resolve")
    assert result["vehicle_state"]["media_playing"] and result["vehicle_state"]["temperature_celsius"] == 25
    assert len(result["steps"]) == 2 and all(s["status"] == "completed" for s in result["steps"])
    graph.close()


@pytest.mark.asyncio
async def test_model_can_understand_context_clause_without_extra_capability(services, tmp_path):
    model = ScriptedModel([{"act": "request", "actions": [{"capability": "media.play"}]}])
    graph, _, _ = build(services, tmp_path, model)
    result = await invoke(graph, "tôi thấy buồn và bật nhạc", "context-clause")
    assert result["vehicle_state"]["media_playing"] and result["diagnostics"]["effective_provider"] == "google"
    graph.close()


@pytest.mark.asyncio
async def test_audit_memory_name_and_partial_location(services, tmp_path):
    graph, _, _ = build(services, tmp_path)
    await invoke(graph, "lưu tên tôi", "1")
    result = await invoke(graph, "nhớ tên tôi", "2")
    assert result["status"] == "clarify" and services.memory.list("default") == []
    result = await invoke(graph, "An", "3")
    assert services.memory.list("default")[0]["value"] == "An"
    await invoke(graph, "mở cửa", "4")
    await invoke(graph, "cửa kính", "5")
    result = await invoke(graph, "trái", "6")
    assert result["status"] == "clarify"
    result = await invoke(graph, "sau", "7")
    assert result["status"] == "confirmation_required"
    assert result["action_proposal"]["arguments"]["zone"] == "rear_left"
    assert result["action_proposal"]["arguments"]["position_percent"] == 100
    graph.close()


@pytest.mark.asyncio
async def test_audit_compound_slot_then_continue_and_repeat(services, tmp_path):
    graph, _, _ = build(services, tmp_path)
    result = await invoke(graph, "tăng điều hoà lên và bật nhạc", "1")
    assert all(step["status"] == "completed" for step in result["steps"]) and len(result["steps"]) == 2
    result = await invoke(graph, "tăng 2 độ", "2")
    assert all(step["status"] == "completed" for step in result["steps"])
    assert result["vehicle_state"]["temperature_celsius"] == 27
    result = await invoke(graph, "tăng tiếp", "3")
    assert result["vehicle_state"]["temperature_celsius"] == 29
    await invoke(graph, "tăng điều hoà", "4")
    result = await invoke(graph, "tăng một chút tuỳ bạn", "5")
    assert result["vehicle_state"]["temperature_celsius"] == 30
    result = await invoke(graph, "tăng tiếp", "6")
    assert result["vehicle_state"]["temperature_celsius"] == 30
    graph.close()


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


@pytest.mark.parametrize("tier", ["google", "local"])
@pytest.mark.asyncio
async def test_provider_guard_repairs_audit_failures(services, tmp_path, tier):
    bad = {
        "act": "request",
        "actions": [
            {"capability": "memory.remember", "arguments": {"memory_key": "display_name", "memory_value": "tôi"}}
        ],
    }
    model = ScriptedModel([bad])
    graph, _, _ = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    result = await invoke(graph, "nhớ tên tôi", "1")
    assert services.memory.list("default") == []
    assert result["diagnostics"]["effective_provider"] == tier
    assert result["diagnostics"]["provider_attempts"][-1]["semantic_guard"]
    graph.close()


class Maps:
    def __init__(self):
        self.queries = []

    async def search(self, text, near=None, **kwargs):
        self.queries.append((text, near))
        return [
            Place(ref_id="bad", name="Nhất Tâm Logistics"),
            Place(ref_id="a", name="Nhà thuốc TEST", address="TEST Hanoi", lat=21, lng=105.9),
        ]

    async def route(self, a, b):
        distance = abs(b[1] - a[1]) * 1000000
        return Route(
            distance_m=distance,
            duration_s=distance / 20,
            coordinates=[[a[1], a[0]], [b[1], b[0]]],
            steps=[],
        )

    async def place(self, ref):
        return Place(ref_id=ref, name="Nhà thuốc TEST", lat=21, lng=105.9)

    async def matrix(self, points):
        raise RuntimeError("test route fallback")


@pytest.mark.asyncio
async def test_navigation_preview_restart_consent_and_replay(services, tmp_path):
    services = replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    graph, store, _ = build(services, tmp_path)
    result = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "1",
            "input_text": "tìm hiệu thuốc gần nhất",
            "position": {"lat": 21, "lng": 105.8, "simulated": True},
        }
    )
    nav = result["navigation"]
    assert len(nav["candidates"]) == 1 and nav["candidates"][0]["name"] == "Nhà thuốc TEST"
    assert services.navigation.queries[0][1] == (21, 105.8)
    result = await invoke(graph, "chỗ đầu tiên", "2")
    assert result["status"] == "answered" and not result.get("vehicle_state"), result["output"]
    assert result["navigation"]["status"] == "preview"
    graph.close()
    graph, _, _ = build(services, tmp_path, store=store)
    result = await invoke(graph, "đồng ý", "3")
    assert result["navigation"]["status"] == "running" and result["vehicle_state"]["driving"]
    assert (await invoke(graph, "đồng ý", "3"))["navigation"] == result["navigation"]
    result = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "4",
            "input_text": "progress",
            "navigation_event": {"kind": "progress", "id": "e1", "revision": nav["revision"], "distance_m": 1000},
        }
    )
    # stale event cannot mutate route
    assert result["navigation"]["progress_m"] == 0
    result = await invoke(graph, "dừng dẫn đường", "5")
    assert result["navigation"]["status"] == "paused"
    graph.close()


@pytest.mark.asyncio
async def test_origin_event_resumes_original_request(services, tmp_path):
    services = replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile()))
    graph, _, _ = build(services, tmp_path)
    result = await invoke(graph, "dẫn tôi đến hiệu thuốc gần nhất", "1")
    assert result["status"] == "clarify", result["output"]
    result = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "2",
            "input_text": "position",
            "navigation_event": {
                "kind": "position",
                "id": "gps",
                "revision": 0,
                "position": {"lat": 21, "lng": 105.8, "simulated": True},
            },
        }
    )
    assert result["status"] == "clarify" and result["navigation"]["status"] == "selecting"
    selected = await invoke(graph, "chỗ đầu tiên", "choose")
    assert selected["status"] == "answered" and selected["navigation"]["status"] == "preview"
    graph.close()
