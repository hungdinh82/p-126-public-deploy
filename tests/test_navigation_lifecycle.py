"""Parking, continuing, replacing and clearing server-owned journeys."""

from dataclasses import replace

import pytest

from src.vivi.navigation.energy import EnergyPlanner
from tests.test_dialogue_v2 import build, invoke
from tests.test_long_term_memory import services as services
from tests.test_navigation_refinement import DestinationMaps
from tests.test_navigation_workflow import profile


async def start(graph):
    await graph.ainvoke(
        {"session_id": "driver", "turn_id": "find", "input_text": "đi Tràng An", "position": {"lat": 21, "lng": 105.85}}
    )
    await invoke(graph, "chỗ đầu tiên", "choose")
    return await invoke(graph, "đồng ý", "start")


async def event(graph, result, kind, identifier, **extra):
    return await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": identifier,
            "navigation_event": {"kind": kind, "id": identifier, "revision": result["navigation"]["revision"], **extra},
        }
    )


def runtime(services, tmp_path, **kwargs):
    return build(
        replace(services, navigation=DestinationMaps(), energy_planner=EnergyPlanner(profile())), tmp_path, **kwargs
    )


@pytest.mark.asyncio
async def test_park_resume_survives_restart_keeps_progress_and_never_replays_start(services, tmp_path):
    graph, store, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        running = await event(graph, running, "progress", "progress", distance_m=5000)
        paused = await event(graph, running, "pause", "park")
        assert paused["navigation"]["status"] == "paused" and paused["navigation"]["resume_status"] == "running"
        assert paused["navigation"]["progress_m"] == 5000 and not gateway.vehicle.state_for("driver").driving
        ignored = await event(graph, paused, "progress", "old-progress", distance_m=6000)
        assert ignored["navigation"]["progress_m"] == 5000 and not gateway.vehicle.state_for("driver").driving
        graph.close()
        graph, _, gateway = runtime(services, tmp_path, store=store)
        resumed = await event(graph, paused, "resume", "drive")
        assert resumed["navigation"]["status"] == "running" and resumed["navigation"]["progress_m"] == 5000
        assert resumed["navigation"]["route_id"] == paused["navigation"]["route_id"]
        assert gateway.vehicle.state_for("driver").driving
        parked_again = await event(graph, resumed, "pause", "park-again")
        replay = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "replay",
                "navigation_event": {
                    "kind": "resume",
                    "id": "drive",
                    "revision": parked_again["navigation"]["revision"],
                },
            }
        )
        assert replay["navigation"]["status"] == "paused" and not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_parking_keeps_new_destination_offer_and_acceptance_replaces_old_route(services, tmp_path):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        old = await start(graph)
        offered = await event(graph, old, "point", "new-place", position={"lat": 21, "lng": 106, "label": "Vinhomes"})
        assert offered["status"] == "answered"
        paused = await event(graph, offered, "pause", "park")
        assert paused["navigation"]["selected"] == old["navigation"]["selected"]
        assert paused["navigation"]["pending"]["navigation"]["selected"]["name"] == "Vinhomes"
        resumed = await event(graph, paused, "resume", "drive")
        assert resumed["navigation"]["selected"] == old["navigation"]["selected"]  # pending consent is separate
        accepted = await invoke(graph, "đồng ý", "accept-new")
        assert (
            accepted["navigation"]["status"] == "running" and accepted["navigation"]["selected"]["name"] == "Vinhomes"
        )
        assert accepted["navigation"]["route"]["coordinates"][-1] == [106, 21]
        assert accepted["navigation"]["route_id"] != old["navigation"]["route_id"]
        assert accepted["navigation"]["pending"] is None and gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_new_preview_after_parking_cannot_inherit_an_older_pending_route(services, tmp_path):
    graph, _, _ = runtime(services, tmp_path)
    try:
        old = await start(graph)
        offered = await event(graph, old, "point", "old-offer", position={"lat": 21, "lng": 106, "label": "Đích cũ"})
        paused = await event(graph, offered, "pause", "park")
        latest = await event(graph, paused, "point", "latest", position={"lat": 21, "lng": 106.1, "label": "Vinhomes"})
        assert latest["navigation"]["status"] == "paused"
        candidate = latest["navigation"]["pending"]["navigation"]
        assert candidate["selected"]["name"] == "Vinhomes" and candidate["pending"] is None
        accepted = await invoke(graph, "đồng ý", "accept-latest")
        assert accepted["navigation"]["selected"]["name"] == "Vinhomes"
        assert accepted["navigation"]["route"]["coordinates"][-1] == [106.1, 21]
    finally:
        graph.close()


@pytest.mark.parametrize("condition", ["hood", "battery"])
@pytest.mark.asyncio
async def test_resume_checks_vehicle_policy_and_energy(services, tmp_path, condition):
    graph, _, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        paused = await event(graph, running, "pause", "park")
        if condition == "hood":
            from src.vivi.domain.models import ActionProposal

            gateway.vehicle.execute_sync(
                "driver", "hood", ActionProposal(intent="hood.set_open", arguments={"open": True})
            )
        else:
            gateway.vehicle.set_energy("driver", 20, 60)
        denied = await event(graph, paused, "resume", "drive")
        assert denied["status"] == "blocked" and denied["navigation"]["status"] == "paused"
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_clear_while_parked_invalidates_grants_and_stale_events_keeps_effect_ledger(services, tmp_path):
    graph, store, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        rejected = await event(graph, running, "clear", "clear-driving")
        assert rejected["status"] == "blocked" and gateway.vehicle.state_for("driver").driving
        offered = await event(graph, running, "point", "offer", position={"lat": 21, "lng": 106, "label": "Vinhomes"})
        grant = offered["confirmation"]["confirmation_id"]
        paused = await event(graph, offered, "pause", "park")
        cleared = await event(graph, paused, "clear", "clear")
        assert cleared["navigation"]["status"] == "idle" and cleared["navigation"]["route"] is None
        assert cleared["trip_plan"] is None and cleared["navigation"]["pending"] is None
        assert cleared["steps"] == [] and cleared["session"]["active"] is None
        stale = await event(graph, paused, "resume", "old-drive")
        assert stale["status"] == "blocked" and not gateway.vehicle.state_for("driver").driving
        denied = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "old-grant",
                "confirmation_id": grant,
                "confirmation_decision": "approve",
            }
        )
        assert denied["status"] == "denied" and not gateway.vehicle.state_for("driver").driving
        assert store.command_result("driver", running["session"]["active"]["steps"][0]["command_id"])
    finally:
        graph.close()


def test_clear_removes_completed_journey_context_but_preserves_other_capabilities():
    from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SessionState, TaskStep, TripPlanState
    from src.vivi.dialogue.reducer import clear_journey

    session = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(draft=ActionDraft(capability="media.play"), status="completed"),
                TaskStep(
                    draft=ActionDraft(capability="navigation.start", arguments={"route_revision": 1}),
                    status="completed",
                    result={"navigation_update": {"route": "old"}},
                ),
            ]
        ),
        suspended=[
            DialogueTask(
                steps=[
                    TaskStep(
                        draft=ActionDraft(capability="navigation.plan", arguments={"query": "Vinhomes"}),
                        status="waiting_slot",
                    )
                ]
            )
        ],
        trip_plan=TripPlanState(),
    )
    cleared = clear_journey(session)
    assert [step.draft.capability for step in cleared.active.steps] == ["media.play"]
    assert cleared.active.steps[0].status == "completed"
    assert not cleared.suspended and cleared.trip_plan is None
