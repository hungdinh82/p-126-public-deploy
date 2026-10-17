"""Focused log 974/984/986–991 replay; all map routes are labelled fixtures."""

import pytest

from src.vivi.dialogue.contracts import SessionState
from eval.legacy.grammar import OfflineParser
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import Place, VietMapError
from tests.support.dialogue_v2 import ScriptedModel, build, invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile, station
from tests.support.trip_plans import CHARGER as CHARGER
from tests.support.trip_plans import DEST as DEST
from tests.support.trip_plans import ORIGIN as ORIGIN
from tests.support.trip_plans import TripMaps as TripMaps
from tests.support.trip_plans import runtime as runtime


class ContextModel:
    """Mock semantic responses use handles supplied by the server, no parser."""

    async def agenerate_json(self, **kwargs):
        import json

        prompt = json.loads(kwargs["user"])
        text = prompt["utterance"]
        plan = prompt["tasks"].get("trip_plan")
        args = {"plan_id": plan["id"], "route_revision": plan["revision"]} if plan else {}
        if text.startswith("lập"):
            name = "trip.plan"
            args = {"query": "Ninh Bình", "trip_kind": "one_way", "destination_scope": "region"}
        elif plan.get("clarification"):
            choices = plan["clarification"]["choices"]
            name = "trip.refine"
            if text == "Tràng An":
                args["query"] = "Tràng An"
            else:
                args["choice_handle"] = next(
                    c["handle"] for c in choices if c["kind"] == ("specific" if text == "có" else "representative")
                )
        elif text == "đúng":
            name = "trip.select"
            args["place_handle"] = plan["candidates"][0]["handle"]
        else:
            name = "trip.decline" if text == "không" else "trip.start"
        return json.dumps({"act": "request", "actions": [{"capability": name, "arguments": args}]}, ensure_ascii=False)




@pytest.mark.parametrize(
    "text,query,kind",
    [
        ("lập lộ trình sạc đi vào Huế", "Huế", "one_way"),
        ("lập lộ trình sạc từ đây về Đà Nẵng", "Đà Nẵng", "one_way"),
        ("lập lộ trình đi và sạc từ đây về Nghi Sơn Thanh Hoá và quay lại", "Nghi Sơn Thanh Hoá", "round_trip"),
    ],
)
def test_logged_queries_are_names_only(text, query, kind):
    action = OfflineParser().interpret(text, SessionState()).actions[0]
    assert action.capability == "trip.plan"
    assert action.arguments.query == query and action.arguments.trip_kind == kind


def test_model_query_not_replaced_by_parser():
    from src.vivi.dialogue.contracts import SemanticTurn

    proposal = SemanticTurn(
        act="request",
        actions=[
            {
                "capability": "trip.plan",
                "arguments": {"query": "Nghi Sơn, Thanh Hoá", "trip_kind": "round_trip", "destination_scope": "region"},
            }
        ],
    )
    assert (
        OfflineParser().guard(
            "lập lộ trình đi và sạc từ đây về Nghi Sơn Thanh Hoá và quay lại", proposal, SessionState()
        )
        == proposal
    )


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_default_center_yes_no_and_plan_not_driving(services, tmp_path, tier):
    services, maps = runtime(services)
    model = ContextModel()
    graph, _, gateway = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch đi và sạc tới Ninh Bình",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        assert first["trip_plan"]["route"] is not None
        assert first["trip_plan"]["clarification"]["phase"] == "confirm_destination"
        assert first["navigation"]["status"] == "idle" and not gateway.vehicle.state_for("driver").driving
        accepted = await invoke(graph, "OK", "keep")
        assert accepted["trip_plan"]["calculation_status"] == "ready", accepted
        assert accepted["steps"][0]["status"] == "completed"
        assert accepted["trip_plan"]["offer"]["status"] == "pending"
        declined = await invoke(graph, "không", "decline-start")
        assert declined["trip_plan"]["route"] and not gateway.vehicle.state_for("driver").driving
        started = await invoke(graph, "bắt đầu luôn", "start")
        assert started["navigation"]["status"] == "running", started
        assert gateway.vehicle.state_for("driver").driving
        assert started["trip_plan"]["started_revision"] == started["trip_plan"]["revision"]
        replay = await invoke(graph, "bắt đầu luôn", "replay")
        assert replay["navigation"] == started["navigation"]
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_center_change_specific_single_question(services, tmp_path):
    services, maps = runtime(services)
    graph, _, _ = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch tới Ninh Bình",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        other = await invoke(graph, "đổi địa điểm", "other")
        assert other["trip_plan"]["clarification"]["phase"] == "specific"
        named = await invoke(graph, "Tràng An", "name")
        assert maps.queries[-1][0] == "Tràng An, Ninh Bình"
        assert "Đúng chỗ này chứ?" in named["response_text"]
        done = await invoke(graph, "đúng", "accept")
        assert done["trip_plan"]["calculation_status"] == "ready", done
    finally:
        graph.close()


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


@pytest.mark.asyncio
async def test_model_mock_plan_preserves_return_and_running_navigation(services, tmp_path):
    services, maps = runtime(services, 65)
    proposal = {
        "act": "request",
        "actions": [
            {
                "capability": "trip.plan",
                "arguments": {"query": "Nghi Sơn, Thanh Hoá", "trip_kind": "round_trip", "destination_scope": "region"},
            }
        ],
    }
    graph, _, _ = build(services, tmp_path, ScriptedModel([proposal]))
    graph.default_provider = "google"
    try:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "991",
                "input_text": "lập lộ trình đi và sạc từ đây về Nghi Sơn Thanh Hoá và quay lại",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        trip = result["trip_plan"]
        assert trip["trip_kind"] == "round_trip" and len(trip["waypoint_routes"]) == 2
        assert trip["energy"]["selected_plan"]["stops"]
        assert trip["route"]["distance_m"] == 270000
        assert result["navigation"]["status"] == "idle"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_default_region_correction_invalidates_old_choices_and_keeps_return(services, tmp_path):
    services, maps = runtime(services, 65)
    graph, _, _ = build(services, tmp_path)
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch đi và sạc đến Ninh Bình và quay lại",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        old = first["trip_plan"]
        changed = await invoke(graph, "không, về Thanh Hoá", "change-region")
        new = changed["trip_plan"]
        assert new["clarification"]["region"] == "Thanh Hoá", changed
        assert new["trip_kind"] == "round_trip" and len(new["waypoint_routes"]) == 2
        assert new["revision"] > old["revision"]
        assert maps.queries[-1][0] == "Thanh Hoá"
        stale = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "stale",
                "action": {
                    "capability": "trip.refine",
                    "arguments": {
                        "plan_id": old["id"],
                        "route_revision": old["revision"],
                        "choice_handle": old["clarification"]["choices"][0]["handle"],
                    },
                },
            }
        )
        assert stale["trip_plan"]["revision"] == new["revision"] and not (stale.get("execution") or {}).get(
            "executed", False
        )
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_plan_completes_and_remaining_music_runs_without_driving(services, tmp_path):
    services, _ = runtime(services)
    graph, _, gateway = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch tới Ninh Bình rồi bật nhạc",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        done = await invoke(graph, "cứ lấy trung tâm", "center")
        assert all(step["status"] == "completed" for step in done["steps"]), done
        assert gateway.vehicle.state_for("driver").media_playing and not gateway.vehicle.state_for("driver").driving
        assert done["response_text"].count("?") == 1
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_energy_change_updates_offer_without_start_and_restart_deduplicates(services, tmp_path):
    services, _ = runtime(services)
    graph, store, gateway = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch tới Ninh Bình",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        kept = await invoke(graph, "OK", "keep")
        gateway.vehicle.set_energy("driver", 70, 200)
        updated = await invoke(graph, "đồng ý", "start-changed")
        assert updated["trip_plan"]["revision"] > kept["trip_plan"]["revision"]
        assert not gateway.vehicle.state_for("driver").driving
        assert "cập nhật kế hoạch" in updated["response_text"]
        final = await invoke(graph, "đồng ý", "start-updated")
        assert final["navigation"]["status"] == "running"
        graph.close()
        graph, _, _ = build(services, tmp_path, store=store)
        replay = await invoke(graph, "bắt đầu luôn", "restart-replay")
        assert replay["navigation"]["status"] == "paused"
        assert replay["navigation"]["route_id"] == final["navigation"]["route_id"]
        assert not gateway.vehicle.state_for("driver").driving
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM command_ledger WHERE turn LIKE '%:start'").fetchone()[0] == 1
    finally:
        graph.close()


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


@pytest.mark.asyncio
async def test_missing_return_route_never_reports_no_charging(services, tmp_path):
    services, maps = runtime(services)
    original_route = maps.route

    async def no_return(a, b):
        if a == DEST and b == ORIGIN:
            raise VietMapError("Missing return route fixture")
        return await original_route(a, b)

    maps.route = no_return
    graph, _, gateway = build(services, tmp_path)
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch tới Ninh Bình và quay lại",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        assert first["trip_plan"]["calculation_status"] == "incomplete"
        assert (
            "chưa hoàn chỉnh" in first["response_text"].lower() and "Không cần dừng sạc" not in first["response_text"]
        )
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_trip_plan_does_not_overwrite_running_navigation(services, tmp_path):
    services, _ = runtime(services)
    graph, _, gateway = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "nav",
                "input_text": "chỉ đường đến Tràng An",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        await invoke(graph, "đúng", "place")
        started = await invoke(graph, "đồng ý", "start")
        assert started["navigation"]["status"] == "running"
        planning = await invoke(graph, "lập kế hoạch tới Ninh Bình và quay lại", "trip")
        assert planning["navigation"] == started["navigation"]
        assert planning["trip_plan"]["trip_kind"] == "round_trip"
        assert gateway.vehicle.state_for("driver").driving
        kept = await invoke(graph, "Giữ địa điểm", "center")
        assert kept["navigation"] == started["navigation"]
        assert kept["trip_plan"]["calculation_status"] == "ready"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_cancel_default_plan_preserves_unrelated_steps_without_starting_a_route(services, tmp_path):
    services, _ = runtime(services)
    graph, _, gateway = build(services, tmp_path)
    try:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch tới Ninh Bình rồi bật nhạc",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        plan = result["trip_plan"]
        cancelled = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "cancel",
                "action": {
                    "capability": "trip.cancel",
                    "arguments": {"plan_id": plan["id"], "route_revision": plan["revision"]},
                },
            }
        )
        assert cancelled["trip_plan"]["calculation_status"] == "cancelled"
        assert cancelled["steps"][0]["status"] == "cancelled"
        assert cancelled["steps"][1]["capability"] == "media.play"
        assert cancelled["steps"][1]["status"] == "completed"
        assert gateway.vehicle.state_for("driver").media_playing
    finally:
        graph.close()


def test_model_cannot_grant_start_from_a_question_or_change_destination_yes():
    from src.vivi.dialogue.contracts import SemanticTurn, TripPlanState

    plan = TripPlanState(
        id="trip:consent",
        revision=1,
        calculation_status="ready",
        offer={"question": "Bạn muốn bắt đầu hành trình này không?", "status": "pending"},
    )
    state = SessionState(trip_plan=plan)
    start = SemanticTurn(
        act="request", actions=[{"capability": "trip.start", "arguments": {"plan_id": plan.id, "route_revision": 1}}]
    )
    parser = OfflineParser()
    assert parser.guard("tổng cộng bao nhiêu phút sạc", start, state).act == "clarify"
    assert parser.guard("đồng ý", start, state) == start
    plan.clarification = {
        "phase": "change_destination",
        "question": "Bạn muốn đổi điểm đến không?",
        "revision": 1,
        "choices": [],
    }
    assert parser.guard("có", start, state).act == "clarify"
    assert parser.guard("bắt đầu luôn", start, state) == start


@pytest.mark.asyncio
async def test_origin_event_resumes_trip_without_opening_or_starting(services, tmp_path):
    services, _ = runtime(services)
    graph, _, gateway = build(services, tmp_path)
    try:
        first = await invoke(graph, "lập kế hoạch tới Ninh Bình", "no-position")
        assert first["steps"][0]["status"] == "waiting_slot"
        assert any(e["type"] == "trip.locate" for e in first["ui_events"])
        positioned = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "position",
                "navigation_event": {
                    "kind": "position",
                    "id": "position",
                    "revision": first["navigation"]["revision"],
                    "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
                },
            }
        )
        assert positioned["trip_plan"]["route"] and positioned["trip_plan"]["clarification"]
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_log_986_to_989_center_other_single_match_and_preview(services, tmp_path):
    services, maps = runtime(services)
    graph, _, gateway = build(services, tmp_path)
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "986",
                "input_text": "chỉ đường đến Đà Nẵng",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        assert first["navigation"]["clarification"]["phase"] == "confirm_destination"
        handle = first["navigation"]["clarification"]["choices"][1]["handle"]
        other = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "987",
                "action": {"capability": "navigation.refine", "arguments": {"choice_handle": handle}},
            }
        )
        assert other["navigation"]["clarification"]["phase"] == "specific"
        found = await invoke(graph, "bãi biển sơn trà", "988")
        assert maps.queries[-1][0] == "bãi biển sơn trà, Đà Nẵng"
        assert "Đúng chỗ này chứ?" in found["response_text"] and "chọn số mấy" not in found["response_text"]
        preview = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "989",
                "action": {
                    "capability": "navigation.select",
                    "arguments": {"place_handle": found["navigation"]["candidates"][0]["handle"]},
                },
            }
        )
        assert preview["status"] == "answered"
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_unrelated_map_result_asks_for_more_evidence_instead_of_success(services, tmp_path):
    services, maps = runtime(services)

    async def unrelated(query, **kwargs):
        return [Place(ref_id="wrong", name="Chùa Bái Đính", address="Ninh Bình", lat=DEST[0], lng=DEST[1])]

    maps.search = unrelated
    graph, _, gateway = build(services, tmp_path)
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "first",
                "input_text": "lập kế hoạch đến Tràng An, Ninh Bình",
                "position": {"lat": ORIGIN[0], "lng": ORIGIN[1]},
            }
        )
        assert first["steps"][0]["status"] == "waiting_slot"
        assert first["trip_plan"]["candidates"] == [] and first["trip_plan"]["selected"] is None
        assert first["trip_plan"]["clarification"]["phase"] == "specific"
        assert "địa chỉ khác" in first["response_text"] and not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()
