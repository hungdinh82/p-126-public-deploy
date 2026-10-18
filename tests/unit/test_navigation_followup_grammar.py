"""Cancel replies and navigation follow-ups never depend on the small model."""

import pytest

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.fallback import literal_cancel
from src.vivi.dialogue.reducer import reduce_turn
from src.vivi.dialogue.tool_discovery import matched_capabilities
from src.vivi.navigation.models import NavigationWorkspace, PlanDraft


def with_draft():
    session = SessionState()
    session.navigation_workspace = NavigationWorkspace(revision=3, draft=PlanDraft(status="preview", revision=3))
    return session


@pytest.mark.parametrize("text", ["huỷ", "Hủy.", "huỷ kế hoạch", "thôi huỷ đi", "không đi nữa", "huỷ tuyến nhé"])
def test_cancel_phrases_cancel_open_plan(text):
    turn = literal_cancel(text, with_draft())
    assert turn and turn.act == "cancel" and turn.task_ref == "active"


@pytest.mark.parametrize("text", ["huỷ sạc rồi tìm cà phê", "Huy ơi bật nhạc"])
def test_cancel_grammar_never_swallows_requests(text):
    assert literal_cancel(text, with_draft()) is None


def test_cancel_without_anything_open_goes_to_the_model():
    assert literal_cancel("huỷ", SessionState()) is None


def test_cancel_without_task_still_cancels_orphan_draft():
    session = with_draft()
    state, message = reduce_turn(session, literal_cancel("huỷ kế hoạch", session))
    assert message == "Đã hủy kế hoạch đang chờ."
    assert state.domain_commands[-1] == {"kind": "cancel_draft", "plan_id": session.navigation_workspace.draft.id}


@pytest.mark.parametrize(
    "text,capability",
    [
        ("đổi về Đồ Sơn", "navigation.refine"),
        ("có chỗ nào gần hơn không?", "navigation.refine"),
        ("quán ở gần vị trí hiện tại của tôi nhất?", "navigation.search"),
        ("chỉ đường tới bệnh viện gần nhất đi", "navigation.search"),
        ("huỷ kế hoạch", "navigation.cancel"),
    ],
)
def test_discovery_offers_navigation_tools(text, capability):
    names = {c["name"] for c in matched_capabilities(text, catalog(navigation=True))}
    assert capability in names
    assert "web.search" not in names


def test_nearer_over_a_nearest_list_repeats_it_with_distances():
    from src.vivi.dialogue.fallback import literal_nearer
    from src.vivi.navigation.contracts import DestinationSpec

    session = with_draft()
    draft = session.navigation_workspace.draft
    draft.destination = DestinationSpec.from_arguments({"query": "trạm sạc gần nhất"})
    draft.candidates = [
        {"name": "Trạm Sạc Times City", "address": "Phường Vĩnh Tuy", "category": "charging", "distance_m": 410}
    ]
    turn = literal_nearer("có chỗ nào gần hơn không?", session)
    assert turn.act == "reply" and turn.text.startswith("Đây đã là các trạm sạc gần xe nhất.")
    assert "400 m" in turn.text
    assert literal_nearer("có chỗ nào gần hơn không?", SessionState()) is None


def test_unspoken_origin_is_dropped_from_a_destination_update():
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.planner import SemanticResolver

    turn = SemanticTurn(
        act="request",
        actions=[
            {
                "capability": "navigation.plan",
                "arguments": {"query": "Phường Lê Chân", "origin_query": "Phường Đồ Sơn"},
            }
        ],
    )
    value = SemanticResolver.destination_slots(turn, "Phường Lê Chân", SessionState())
    assert value.actions[0].arguments.origin_query is None
    spoken = turn.model_copy(deep=True)
    spoken.actions[0].arguments.origin_query = "Phường Đồ Sơn"
    value = SemanticResolver.destination_slots(spoken, "đi từ Phường Đồ Sơn tới Phường Lê Chân", SessionState())
    assert value.actions[0].arguments.origin_query == "Phường Đồ Sơn"


@pytest.mark.parametrize(
    "text,index",
    [("chọn cái thứ nhất", 1), ("cái thứ nhất", 1), ("số 2", 2), ("thứ hai", 2), ("cái cuối cùng", 3), ("Nam", None)],
)
def test_spoken_ordinals_pick_a_choice(text, index):
    from src.vivi.dialogue.fallback import choice_index
    from src.vivi.text import normalize_text

    assert choice_index(normalize_text(text), 3) == index


def test_cancel_current_route_with_only_a_live_trip_ends_it():
    from src.vivi.navigation.models import ActiveJourney

    session = SessionState()
    session.navigation_workspace = NavigationWorkspace(
        journey=ActiveJourney(
            status="running", route={"coordinates": [[105.8, 21], [105.9, 21]], "distance_m": 1, "duration_s": 1}
        )
    )
    turn = literal_cancel("huỷ lộ trình hiện tại", session)
    assert turn.act == "request" and [a.capability for a in turn.actions] == ["navigation.end"]
    assert literal_cancel("huỷ", session) is None


def test_new_directions_request_over_an_open_plan_is_not_a_refine():
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.local_operations import fresh_navigation_request

    proposed = SemanticTurn(
        act="request",
        actions=[
            {
                "capability": "navigation.refine",
                "arguments": {"query": "trạm sạc gần nhất", "choice_handle": "place:old", "reject_candidates": True},
            }
        ],
    )
    capabilities = catalog(navigation=True)
    fresh = fresh_navigation_request(proposed, "chỉ đường tới trạm sạc gần nhất", capabilities)
    assert fresh.actions[0].capability == "navigation.plan"
    assert fresh.actions[0].arguments.query == "trạm sạc gần nhất"
    assert fresh.actions[0].arguments.choice_handle is None
    kept = fresh_navigation_request(proposed, "đổi về trạm sạc gần nhất", capabilities)
    assert kept.actions[0].capability == "navigation.refine"


def test_directions_grammar_overrides_a_model_refusal_only_for_plain_requests():
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.local_operations import directions_from_grammar

    refusal = SemanticTurn(act="clarify", text="Bạn nói rõ yêu cầu muốn thực hiện nhé.")
    capabilities = catalog(navigation=True)
    plan = directions_from_grammar(refusal, "chỉ đường tới Phủ Lý", capabilities)
    assert plan.act == "request" and plan.actions[0].capability == "navigation.plan"
    assert plan.actions[0].arguments.query == "Phủ Lý"
    for text in ["chỉ đường tới Phủ Lý rồi bật nhạc", "chỉ đường tới đâu đó và về", "bật điều hoà"]:
        assert directions_from_grammar(refusal, text, capabilities) is refusal


def test_category_without_query_and_unspoken_round_trip_are_normalised():
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.planner import SemanticResolver

    turn = SemanticTurn(
        act="request",
        actions=[{"capability": "navigation.search", "arguments": {"category": "hospital", "nearest": True}}],
    )
    value = SemanticResolver.destination_slots(turn, "chỉ đường tới bệnh viện gần nhất", SessionState())
    assert value.actions[0].arguments.query == "bệnh viện"
    plan = SemanticTurn(
        act="modify",
        step_updates=[
            {
                "step_index": 0,
                "action": {
                    "capability": "navigation.plan",
                    "arguments": {"query": "Đồ Sơn", "trip_kind": "round_trip"},
                },
            }
        ],
    )
    value = SemanticResolver.destination_slots(plan, "đổi về Đồ Sơn", SessionState())
    assert value.step_updates[0].action.arguments.trip_kind == "one_way"
    plan.step_updates[0].action.arguments.trip_kind = "round_trip"
    value = SemanticResolver.destination_slots(plan, "đi Đồ Sơn khứ hồi", SessionState())
    assert value.step_updates[0].action.arguments.trip_kind == "round_trip"
