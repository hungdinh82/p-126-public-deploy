"""Replay failures 1354–1379 with synthetic maps and no driving consent."""

from dataclasses import replace

import pytest

from eval.legacy.grammar import OfflineParser, place_query_matches
from src.vivi.capabilities.registry import merge_arguments
from src.vivi.conversation.context import resolve_query, topic_subject
from src.vivi.dialogue.answers import AnswerResolver
from src.vivi.dialogue.contracts import ActionDraft, SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.dialogue.reducer import synchronize_question
from src.vivi.navigation.contracts import AreaConstraint, DestinationSpec
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.resolver import PlaceResolver, satisfies_area
from src.vivi.navigation.vietmap import Place, Route
from tests.support.conversation_generalization import pending
from tests.support.dialogue_v2 import build
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile


@pytest.mark.parametrize("query", ["Chỉ đường đi tới Hải Phòng.", "Ừm, chỉ đường đi tới Hải Phòng.", "Ờ. Chỉ đường đi tới Hải Phòng."])
@pytest.mark.asyncio
async def test_hesitation_and_repeated_motion_are_not_destination_words(query):
    resolver = SemanticResolver()
    result, provider, _ = await resolver.resolve(query, SessionState())
    action = result.actions[0]
    assert provider == "rules" and action.capability == "navigation.plan"
    assert action.arguments.query == "Hải Phòng" and action.arguments.destination_scope == "region"


@pytest.mark.parametrize("query", [
    "Chỉ đường tới Hạ Long và giới thiệu qua về chỗ đấy",
    "Chỉ đường tới Thành phố Sao Mai và giới thiệu về nơi ấy",
])
def test_directions_and_overview_are_two_ordered_steps_with_bound_subject(query):
    result = OfflineParser().interpret(query, SessionState())
    assert result.act == "request"
    assert [a.capability for a in result.actions] == ["navigation.plan", "knowledge.lookup"]
    target = result.actions[0].arguments.query
    assert "giới thiệu" not in target
    assert target in result.actions[1].arguments.query and "đấy" not in result.actions[1].arguments.query
    assert not result.unresolved


def test_compound_shortcut_does_not_drop_a_read_step_or_unknown_clause():
    parser = OfflineParser()
    query = "Chỉ đường tới Hạ Long và giới thiệu qua về chỗ đấy"
    assert parser.explicit_navigation_reference(query, SessionState()) is None
    malformed = parser.interpret(query + " rồi gọi cứu hộ", SessionState())
    assert malformed.act == "clarify" and malformed.unresolved


@pytest.mark.parametrize("owner", ["navigation", "trip"])
def test_plain_query_replacement_removes_stale_structured_destination(owner):
    old = ActionDraft(capability=f"{owner}.refine", arguments={"query": "Chị, Nam Định",
        "destination": DestinationSpec(name="Chị", area=AreaConstraint(text="Nam Định"))})
    incoming = ActionDraft(capability=f"{owner}.refine", arguments={"query": "Hồ Vị Xuyên, Nam Định"})
    merged = merge_arguments(old, incoming)
    assert merged["query"] == "Hồ Vị Xuyên, Nam Định" and "destination" not in merged
    assert ActionDraft(capability=f"{owner}.refine", arguments=merged)


def test_explicit_directions_do_not_inherit_previous_default_region():
    state = synchronize_question(pending())
    action = OfflineParser().interpret("Chỉ đường tới Hồ Gươm.", state).actions[0]
    assert action.capability == "navigation.refine" and action.arguments.query == "Hồ Gươm"
    assert action.arguments.destination.area is None


@pytest.mark.parametrize("query", ["Đổi tới Phường Hoà Bình", "Đổi điểm đến sang Hồ Gươm", "Thay điểm đến về Phường Yên Bái"])
def test_destination_correction_verb_is_not_sent_to_maps(query):
    state = synchronize_question(pending())
    action = OfflineParser().interpret(query, state).actions[0]
    assert action.capability == "navigation.refine"
    assert action.arguments.query in {"Phường Hoà Bình", "Hồ Gươm", "Phường Yên Bái"}
    assert action.arguments.destination is None or action.arguments.destination.area is None


def test_truncated_pronoun_is_not_a_new_place_name():
    state = pending(phase="specific")
    state.navigation_workspace.draft.clarification["choices"] = []
    state.navigation_workspace.draft.destination = DestinationSpec(name="Hồ Vị Xuyên", area=AreaConstraint(text="Nam Định"))
    state = synchronize_question(state)
    result = AnswerResolver().resolve("Chị.", state)
    assert result.act == "clarify" and result.actions == []
    assert state.navigation_workspace.draft.destination.name == "Hồ Vị Xuyên"


@pytest.mark.asyncio
async def test_literal_cancellation_uses_current_state_without_calling_the_model():
    class Model:
        async def agenerate_json(self, **kwargs):
            raise AssertionError("A literal cancellation must not wait for a provider")

    resolver = SemanticResolver({"google": ModelProvider("google", Model(), 10)})
    result, provider, attempts = await resolver.resolve("huỷ", pending(), requested="google")
    assert provider == "rules" and result.actions[0].capability == "navigation.cancel"
    assert result.task_ref == "active" and attempts[0]["reference"] == "task_cancellation"


def test_provider_boundary_short_name_matches_a_renamed_parent_area():
    place = {"name": "Hồ Vị Xuyên", "address": "Phường Nam Định, Tỉnh Ninh Bình", "boundaries": [
        {"name": "Nam Định", "full_name": "Phường Nam Định"},
        {"name": "Ninh Bình", "full_name": "Tỉnh Ninh Bình"},
    ]}
    assert satisfies_area(place, AreaConstraint(text="Nam Định"))
    assert not satisfies_area(place, AreaConstraint(text="Hà Nội"))
    assert place_query_matches("Hồ Vị Xuyên, Nam Định", place)
    assert not place_query_matches("Hồ Vị Xuyên, Nam Định", {**place, "name": "Hồ Vị Hoàng"})


@pytest.mark.parametrize("query,name,address", [
    ("Hồ Vị Xuyên, Nam Định", "Vị Xuyên", "Hồ Vị Xuyên, Phường Nam Định, Tỉnh Ninh Bình"),
    ("Quán phở, Hà Nội", "Phở Gia Truyền", "Quận Hoàn Kiếm, Hà Nội"),
])
def test_type_descriptors_and_category_queries_are_not_distinctive_venue_names(query, name, address):
    assert place_query_matches(query, {"name": name, "address": address})


@pytest.mark.parametrize("address,expected_calls", [
    ("Phường Đông, Nam Định", ["Hồ Nguyệt, Nam Định"]),
    ("Phường Đông, Hà Nội", ["Hồ Nguyệt, Nam Định", "Hồ Nguyệt"]),
])
@pytest.mark.asyncio
async def test_context_is_a_search_preference_and_can_fall_back_without_wrong_area(address, expected_calls):
    class Search:
        calls = []

        async def search(self, query, **kwargs):
            self.calls.append(query)
            return [Place(ref_id="moon", name="Hồ Nguyệt", address=address, lat=21, lng=105)]

    client = Search()
    spec = DestinationSpec(name="Hồ Nguyệt", area=AreaConstraint(text="Nam Định", source="context", constraint="preferred"))
    result = await PlaceResolver(client).resolve(spec, {"lat": 21, "lng": 105})
    assert client.calls == expected_calls and result.candidates[0]["address"] == address


@pytest.mark.parametrize("query", ["Hà Nội dân số bao nhiêu", "Hải Phòng diện tích bao nhiêu", "Thành phố Sao Mai dân số bao nhiêu"])
def test_subject_before_requested_attribute_survives_information_followup(query):
    subject = query.split(" dân số")[0].split(" diện tích")[0]
    assert topic_subject(query) == subject
    state = SessionState(conversation_context={"capability": "web.search", "query": query, "subject": topic_subject(query)})
    action = OfflineParser().interpret("ở đấy có gì vui", state).actions[0]
    assert action.capability == "web.search" and subject in action.arguments.query
    assert resolve_query("ở đấy có gì vui", state, topic_reference=True) == "ở " + subject + " có gì vui"


class Maps:
    def __init__(self):
        self.queries = []

    async def search(self, query, **kwargs):
        self.queries.append(query)
        if query == "Hồ Hoàn Kiếm, Hà Nội":
            return [Place(ref_id="lake", name="Hồ Hoàn Kiếm", address="Phường Hoàn Kiếm, Hà Nội", lat=21.01, lng=105.9)]
        return []

    async def route(self, a, b):
        return Route(distance_m=2000, duration_s=300, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


@pytest.mark.asyncio
async def test_new_directions_escape_the_previous_area_and_cancel_revokes_start_offer(services, tmp_path):
    maps = Maps()
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "bad", "action": {
            "capability": "navigation.plan", "arguments": {"query": "Bến Tàu Mặt Trăng, Nam Định"}},
            "position": {"lat": 21, "lng": 105}})
        assert first["status"] == "clarify"
        second = await graph.ainvoke({"session_id": "driver", "turn_id": "new", "input_text": "Chỉ đường tới Hồ Gươm"})
        assert maps.queries[-1] == "Hồ Hoàn Kiếm, Hà Nội" and "Nam Định" not in second["response_text"]
        assert second["navigation"]["status"] == "preview"
        assert second["navigation"]["offer"] is not None
        assert not gateway.vehicle.state_for("driver").driving
        cancelled = await graph.ainvoke({"session_id": "driver", "turn_id": "cancel", "input_text": "huỷ"})
        assert cancelled["output"]["diagnostics"]["semantic"]["actions"][0]["capability"] == "navigation.cancel"
        after = await graph.ainvoke({"session_id": "driver", "turn_id": "yes", "input_text": "OK"})
        assert after["status"] == "clarify" and not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_cancelling_compound_directions_revokes_the_route_and_remaining_steps(services, tmp_path):
    graph, _, gateway = build(replace(services, navigation=Maps(), energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "plan", "input_text":
            "Chỉ đường tới Hồ Gươm rồi bật nhạc", "position": {"lat": 21, "lng": 105}})
        assert first["navigation"]["status"] == "preview"
        assert [step["status"] for step in first["steps"]] == ["waiting_confirmation", "pending"], first["response_text"]
        assert not gateway.vehicle.state_for("driver").media_playing
        cancelled = await graph.ainvoke({"session_id": "driver", "turn_id": "cancel", "input_text": "huỷ"})
        assert cancelled["navigation"]["status"] == "idle"
        assert not gateway.vehicle.state_for("driver").media_playing
        saved = await graph.snapshot("driver")
        assert saved["navigation"]["status"] == "idle"
        assert saved["navigation"]["offer"] is None
        after = await graph.ainvoke({"session_id": "driver", "turn_id": "stale-consent", "input_text": "OK"})
        assert after["navigation"]["status"] == "idle"
        assert not gateway.vehicle.state_for("driver").driving
        assert not gateway.vehicle.state_for("driver").media_playing
    finally:
        await graph.aclose()
