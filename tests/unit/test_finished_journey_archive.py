"""A finished journey is history; it never shadows a newer plan or search."""

from types import SimpleNamespace

import pytest

from src.vivi.conversation.context import destination
from src.vivi.dialogue.contracts import SessionState
from src.vivi.navigation.models import ActiveJourney, NavigationWorkspace, PlanDraft
from src.vivi.navigation.transitions import NavigationTransitions
from tests.support.long_term_memory import services as services

ROUTE = {"coordinates": [[105.8, 21], [105.9, 20]], "distance_m": 120000, "duration_s": 6000, "steps": []}
KIM_SON = {"name": "Xã Kim Sơn", "lat": 20, "lng": 105.9, "handle": "place:kim-son"}
BACH_KHOA = {"name": "Đại Học Bách Khoa Hà Nội", "lat": 21.005, "lng": 105.843, "handle": "place:bk"}


def journey(status):
    return ActiveJourney(status=status, route_id="route:kim-son", selected=KIM_SON, route=ROUTE, progress_m=500)


def draft(**values):
    return PlanDraft(status="selecting", revision=3, candidates=[BACH_KHOA], **values)


def test_snapshot_prefers_new_draft_over_finished_journey():
    for status in ("ended", "completed"):
        workspace = NavigationWorkspace(revision=3, journey=journey(status), draft=draft())
        nav = workspace.snapshot()
        assert nav.status == "selecting" and nav.selected is None and nav.route is None
        assert nav.candidates == [BACH_KHOA] and nav.pending is None


def test_snapshot_keeps_live_journey_and_reports_draft_as_pending():
    workspace = NavigationWorkspace(revision=3, journey=journey("paused"), draft=draft())
    nav = workspace.snapshot()
    assert nav.status == "paused" and nav.selected["name"] == "Xã Kim Sơn"
    assert nav.pending["navigation"]["candidates"] == [BACH_KHOA]


def test_arrival_stays_visible_until_a_new_plan_exists():
    workspace = NavigationWorkspace(journey=journey("completed"))
    assert not workspace.archive_finished() and workspace.snapshot().status == "completed"
    workspace.draft = draft()
    assert workspace.archive_finished() and workspace.journey is None
    assert [item["id"] for item in workspace.history] == ["route:kim-son"]


def test_stored_ended_journey_is_archived_once():
    workspace = NavigationWorkspace(
        journey=journey("ended"), history=[{"id": "route:kim-son", "destination": KIM_SON, "status": "ended"}]
    )
    assert workspace.archive_finished() and workspace.journey is None
    assert len(workspace.history) == 1 and workspace.snapshot().status == "idle"


def test_place_reference_follows_draft_not_finished_journey():
    session = SessionState()
    session.navigation_workspace = NavigationWorkspace(journey=journey("completed"), draft=draft(selected=BACH_KHOA))
    assert destination(session)["name"] == BACH_KHOA["name"]


@pytest.mark.asyncio
async def test_end_clears_journey_and_keeps_last_position(services):
    session = SessionState()
    session.navigation_workspace = NavigationWorkspace(revision=2, journey=journey("running"))
    result, session = await NavigationTransitions(services.action_gateway.vehicle, None).apply(
        SimpleNamespace(session_id="driver"), session, {"kind": "end", "revision": 2, "id": "end"}
    )
    workspace = session.navigation_workspace
    assert result["status"] == "completed" and workspace.journey is None
    assert workspace.history[-1]["status"] == "ended"
    assert workspace.origin and workspace.origin["lat"] < 21  # moved along the route
    assert result["navigation_update"]["status"] == "idle"


@pytest.mark.asyncio
async def test_gear_d_beside_unconfirmed_route_asks_for_confirmation(services):
    plan = PlanDraft(status="preview", revision=4, selected=BACH_KHOA, route=ROUTE, offer={"status": "pending"})
    session = SessionState()
    session.navigation_workspace = NavigationWorkspace(revision=4, draft=plan)
    result, _ = await NavigationTransitions(services.action_gateway.vehicle, None).apply(
        SimpleNamespace(session_id="driver"), session, {"kind": "gear", "gear": "D", "id": "d"}
    )
    assert result["status"] == "blocked" and result["error_code"] == "plan_unconfirmed"
    assert result["request_action"] == {
        "capability": "navigation.start",
        "arguments": {"plan_id": plan.id, "route_revision": 4},
    }
    assert not (await services.action_gateway.vehicle.get_state("driver")).driving


@pytest.mark.asyncio
@pytest.mark.parametrize("delta,bumped", [(0.000001, False), (0.01, True)])
async def test_position_jitter_keeps_the_plan_revision(delta, bumped):
    from src.vivi.navigation.service import NavigationService

    origin = {"lat": 20.9977, "lng": 105.8656, "label": "Vị trí của bạn"}
    plan = PlanDraft(status="selecting", revision=5, origin=origin, offer={"status": "pending"})
    session = SessionState()
    session.navigation_workspace = NavigationWorkspace(revision=5, origin=origin, draft=plan)
    moved = {**origin, "lat": origin["lat"] + delta}
    result = await NavigationService.draft_event(
        SimpleNamespace(), None, session, {"kind": "position", "position": moved}
    )
    assert (result["draft_update"]["revision"] == 6) is bumped
    assert result["draft_update"]["origin"]["lat"] == moved["lat"]
