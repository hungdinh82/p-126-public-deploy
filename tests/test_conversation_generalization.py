"""Contracts and held-out entity cases; maps and coordinates are synthetic."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.dialogue.answers import AnswerResolver
from src.vivi.dialogue.contracts import (
    DialogueTask,
    NavigationState,
    NavigationWorkspace,
    QuestionReply,
    SessionState,
    TaskStep,
    TripPlanState,
)
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.dialogue.questions import current_question, propose_question
from src.vivi.dialogue.reducer import reduce_turn, synchronize_question
from src.vivi.navigation.contracts import AreaConstraint, DestinationSpec
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.resolver import PlaceResolver, satisfies_area
from src.vivi.navigation.selection import decide_place
from src.vivi.navigation.vietmap import Place, Route, VietMapError
from tests.test_dialogue_v2 import build
from tests.test_long_term_memory import services as services
from tests.test_navigation_workflow import profile


def pending(owner="navigation", phase="confirm_destination"):
    data = dict(status="selecting", revision=4, request_id="request", clarification={
        "phase": phase, "region": "Nam Định", "revision": 4, "question": "Một câu hỏi có wording khác.",
        "choices": [{"handle": "keep", "kind": "representative", "label": "Giữ điểm"},
                    {"handle": "change", "kind": "specific", "label": "Đổi điểm"}],
    })
    task = DialogueTask(steps=[TaskStep(draft={"capability": owner + ".plan", "arguments": {"query": "Nam Định"}}, status="waiting_slot")])
    if owner == "trip":
        return SessionState(active=task, trip_plan=TripPlanState(**data), navigation_focus="trip")
    return SessionState(active=task, navigation=NavigationState(**data))


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.parametrize("phase", ["confirm_destination", "change_destination"])
def test_voice_and_ui_share_declared_proposal_effect(owner, phase):
    state = synchronize_question(pending(owner, phase))
    resolver = AnswerResolver()
    question = current_question(state)
    voice = resolver.resolve("OK", state)
    typed = resolver.resolve("", state, reply=QuestionReply(question_id=question.id, option_id="keep"))
    assert voice == typed and voice.actions[0].capability == owner + ".refine"
    assert voice.actions[0].arguments.choice_handle == "keep"
    assert not any(a.capability.endswith("start") for a in voice.actions)


def test_native_proposal_effects_override_legacy_projection_wording():
    state = pending()
    proposal = propose_question(state)
    proposal = proposal.model_copy(update={"kind": "native_question", "text": "Bạn muốn thay điểm này không?",
                                          "yes_effect": proposal.no_effect, "no_effect": proposal.yes_effect})
    state = synchronize_question(state, proposal)
    resolved = AnswerResolver().resolve("có", state)
    assert resolved.actions[0].arguments.choice_handle == "change"
    assert OfflineParser().guard("có", resolved, state) == resolved


@pytest.mark.parametrize("change", ["expired", "revision", "owner", "option"])
def test_stale_or_foreign_reply_cannot_execute(change):
    state = synchronize_question(pending())
    question = current_question(state)
    option = "keep"
    if change == "expired":
        state.dialogue_question.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "revision":
        state.navigation_workspace.revision += 1
    elif change == "owner":
        state.active.steps[0].draft.capability = "window.set_position"
        state.active.steps[0].status = "waiting_confirmation"
    else:
        option = "not-present"
    value = AnswerResolver().resolve("", state, reply=QuestionReply(question_id=question.id, option_id=option))
    assert value.act == "clarify" and not value.actions


def test_area_reply_changes_only_area_with_provenance():
    state = pending()
    state.navigation_workspace.draft.clarification = {
        "phase": "place_area", "revision": 4, "query": "Bến Tàu Mặt Trăng", "choices": [],
    }
    state = synchronize_question(state)
    question = current_question(state)
    value = AnswerResolver().resolve("", state, reply=QuestionReply(question_id=question.id, value="Nam Định"))
    destination = value.actions[0].arguments.destination
    assert destination.name == "Bến Tàu Mặt Trăng"
    assert destination.area.text == "Nam Định" and destination.area.source == "answer"
    assert destination.area.question_id == question.id


def test_completed_unrelated_task_does_not_reactivate_old_map_question():
    state = pending()
    state.active = DialogueTask(steps=[TaskStep(draft={"capability": "media.play"}, status="completed")])
    assert current_question(state) is None
    assert OfflineParser().interpret("OK", state).act == "clarify"


class Maps:
    def __init__(self, name="Hồ Nguyệt", address="Phường Đông, Nam Định"):
        self.name, self.address = name, address
        self.calls, self.roads = [], []

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return [Place(ref_id="fixture", name=self.name, address=self.address, lat=21, lng=105)]

    async def route(self, a, b):
        self.roads.append((a, b))
        return Route(distance_m=2000, duration_s=300, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


@pytest.mark.parametrize("name", ["Hồ Nguyệt", "Công Viên Bình Minh", "Quảng Trường Sao Bắc"])
@pytest.mark.parametrize("area", ["Nam Định", "Thành phố Sao Mai"])
@pytest.mark.asyncio
async def test_unlisted_names_and_areas_use_the_same_evidence_policy(name, area):
    maps = Maps(name, "Phường Đông, " + area)
    resolver = PlaceResolver(maps)
    spec = DestinationSpec.from_arguments({"query": f"{name}, {area}"})
    result = await resolver.resolve(spec, {"lat": 22, "lng": 104})
    assert result.status == "resolved" and result.selected_handle
    assert maps.roads == []  # Identity resolution has not fetched any road yet.
    assert result.evidence[0]["exact_name"]


@pytest.mark.asyncio
async def test_cache_does_not_share_mutations_or_ui_handles_and_rechecks_scope():
    maps = Maps()
    resolver = PlaceResolver(maps)
    spec = DestinationSpec.from_arguments({"query": "Hồ Nguyệt, Nam Định"})
    origin = {"lat": 21.01, "lng": 105}
    first = await resolver.resolve(spec, origin)
    handle = first.candidates[0]["handle"]
    first.candidates[0]["name"] = "mutated"
    second = await resolver.resolve(spec, origin)
    assert len(maps.calls) == 1
    assert second.candidates[0]["name"] == "Hồ Nguyệt" and second.candidates[0]["handle"] != handle
    wrong = await resolver.resolve(spec.with_area("Hà Nội"), origin)
    assert wrong.status == "no_verified_match" and len(maps.calls) == 2


@pytest.mark.asyncio
async def test_provider_error_is_distinct_from_missing_area_and_cancellation_drains():
    class SlowMaps(Maps):
        drained = False

        async def search(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                self.drained = True

    maps = SlowMaps()
    result = await PlaceResolver(maps, budget_seconds=.03).resolve(DestinationSpec(name="Hồ Nguyệt"), {"lat": 21, "lng": 105})
    assert result.status == "provider_unavailable" and result.errors == ["TimeoutError"]
    assert maps.drained


@pytest.mark.asyncio
async def test_missing_coordinates_with_failed_details_is_provider_failure():
    class MissingMetadata(Maps):
        async def search(self, *args, **kwargs):
            return [Place(ref_id="fixture", name=self.name, address=self.address)]

        async def place(self, ref):
            raise VietMapError("fixture unavailable")

    result = await PlaceResolver(MissingMetadata()).resolve(DestinationSpec(name="Hồ Nguyệt"), {"lat": 21, "lng": 105})
    assert result.status == "provider_unavailable" and result.errors


@pytest.mark.asyncio
async def test_unverified_search_hypothesis_needs_user_selection_even_if_model_proposes():
    class VariantMaps(Maps):
        async def search(self, query, **kwargs):
            self.calls.append(query)
            return [] if query.startswith("Chỗ mới") else [Place(ref_id="fixture", name=self.name, address=self.address, lat=21, lng=105)]

    maps = VariantMaps()
    spec = DestinationSpec(name="Chỗ mới", area=AreaConstraint(text="Nam Định"), search_hypotheses=["Hồ Nguyệt, Nam Định"])
    result = await PlaceResolver(maps).resolve(spec, {"lat": 21, "lng": 105})
    assert result.status == "ambiguous" and len(maps.calls) == 2
    assert result.candidates[0]["match_basis"] == "hypothesis"
    decision = await decide_place(None, "rules", "Hồ Nguyệt, Nam Định", result.candidates)
    assert decision["mode"] == "clarify"


@pytest.mark.asyncio
async def test_total_semantic_budget_reserves_local_fallback():
    class Slow:
        cancelled = False

        async def agenerate_json(self, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                self.cancelled = True

    class Local:
        async def agenerate_json(self, **kwargs):
            return json.dumps({"act": "reply", "text": "Mình nghe bạn."})

    cloud = Slow()
    resolver = SemanticResolver({"google": ModelProvider("google", cloud, 10), "local": ModelProvider("local", Local(), 10)},
                                budget_seconds=.5)
    _, provider, attempts = await asyncio.wait_for(resolver.resolve("thử bộ phân tích", SessionState(), requested="google"), 1.5)
    assert cloud.cancelled and provider == "local"
    assert attempts[0]["reason"] == "TimeoutError" and attempts[1]["outcome"] == "served"


def test_model_area_not_in_utterance_is_only_a_preference():
    from src.vivi.dialogue.contracts import SemanticTurn

    value = SemanticTurn(act="request", actions=[{"capability": "navigation.plan", "arguments": {"destination": {
        "raw_text": "Hồ Nguyệt", "name": "Hồ Nguyệt", "area": {"text": "Hà Nội", "source": "user"},
    }}}])
    actual = SemanticResolver.destination_slots(value, "muốn ghé Hồ Nguyệt", SessionState())
    area = actual.actions[0].arguments.destination.area
    assert area.source == "hypothesis" and area.constraint == "preferred"


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.asyncio
async def test_graph_persists_question_and_ui_reply_equals_voice_without_start(services, tmp_path, owner):
    maps = Maps("Bến Tàu Mặt Trăng")
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        prefix = "chỉ đường tới" if owner == "navigation" else "lập kế hoạch tới"
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "first", "input_text": prefix + " Bến Tàu Mặt Trăng, Nam Định",
                                     "position": {"lat": 21.01, "lng": 105}})
        question = first["pending_question"]
        assert question["purpose"] == "confirm_proposal" and question["owner"] == owner
        assert first["session"]["dialogue_question"]["id"] == question["id"] and maps.roads == []
        response = await graph.ainvoke({"session_id": "driver", "turn_id": "choose", "input_text": "Chọn điểm",
                                       "question_reply": {"question_id": question["id"], "option_id": question["choices"][0]["handle"]}})
        state = response["navigation" if owner == "navigation" else "trip_plan"]
        assert state["selected"]["name"] == maps.name and len(maps.roads) == 1
        assert response["pending_question"]["purpose"] == "approve_execution"
        assert not gateway.vehicle.state_for("driver").driving
        stale = await graph.ainvoke({"session_id": "driver", "turn_id": "stale", "input_text": "Chọn điểm",
                                    "question_reply": {"question_id": question["id"], "option_id": question["choices"][0]["handle"]}})
        assert stale["status"] == "clarify" and not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


def test_required_region_is_not_satisfied_by_street_name_in_other_city():
    place = {"name": "Nhà hàng Mới", "address": "12 Phố Huế, Hà Nội"}
    assert not satisfies_area(place, AreaConstraint(text="Huế"))
    assert satisfies_area(place, AreaConstraint(text="Hà Nội"))


@pytest.mark.parametrize("constraint", ["required", "preferred"])
@pytest.mark.asyncio
async def test_structured_entrance_qualifier_is_not_dropped_from_search(constraint):
    maps = Maps("Trường Sao Mai cổng 1")
    spec = DestinationSpec(name="Trường Sao Mai", qualifiers=["cổng 1"], area=AreaConstraint(text="Nam Định", constraint=constraint))
    result = await PlaceResolver(maps).resolve(spec, {"lat": 21, "lng": 105})
    assert [query for query, _ in maps.calls] == ["Trường Sao Mai cổng 1" + (", Nam Định" if constraint == "required" else "")]
    assert result.candidates and result.candidates[0]["name"] == maps.name


def test_model_cannot_invent_entrance_qualifier():
    from src.vivi.dialogue.contracts import SemanticTurn

    value = SemanticTurn(act="request", actions=[{"capability": "navigation.plan", "arguments": {
        "destination": {"raw_text": "Trường Sao Mai", "name": "Trường Sao Mai", "qualifiers": ["cổng 2"]},
    }}])
    with pytest.raises(ValueError, match="qualifier"):
        SemanticResolver.destination_slots(value, "đưa tôi đến Trường Sao Mai", SessionState())


def test_area_reply_can_keep_qualifier_from_owned_question():
    from src.vivi.dialogue.contracts import SemanticTurn

    state = pending()
    draft = state.navigation_workspace.draft
    draft.destination = DestinationSpec(name="Trường Sao Mai", qualifiers=["cổng 1"])
    draft.clarification = {"phase": "place_area", "revision": 4, "query": draft.destination.query(), "choices": []}
    value = SemanticTurn(act="request", actions=[{"capability": "navigation.refine", "arguments": {
        "destination": {"qualifiers": ["cổng 1"], "area": {"text": "Nam Định"}},
    }}])
    result = SemanticResolver.destination_slots(value, "Nam Định", state)
    assert result.actions[0].arguments.destination.query() == "Trường Sao Mai cổng 1, Nam Định"


def test_semantic_context_never_builds_full_geometry_snapshot(monkeypatch):
    from src.vivi.conversation.context import conversation_view

    class Geometry(list):
        def __iter__(self):
            raise AssertionError("Geometry must not be serialized into semantic context")

    state = SessionState(navigation=NavigationState(status="preview", revision=1,
        selected={"handle": "known", "name": "Bến Tàu Mặt Trăng"}, route={"distance_m": 1000}))
    state.navigation_workspace.draft.route["coordinates"] = Geometry([[105, 21]] * 10000)
    monkeypatch.setattr(NavigationWorkspace, "snapshot", lambda self: (_ for _ in ()).throw(AssertionError("full snapshot")))
    context = SemanticResolver.context(state)
    assert context["navigation"]["route"] == {"distance_m": 1000}
    assert conversation_view(state)["destination"]["handle"] == "known"


def test_changing_question_effect_invalidates_old_reply_but_wording_does_not():
    state = synchronize_question(pending())
    old = current_question(state)
    proposal = propose_question(state).model_copy(update={"text": "Cách hỏi khác"})
    state = synchronize_question(state, proposal)
    assert current_question(state).id == old.id
    proposal = proposal.model_copy(update={"yes_effect": proposal.no_effect})
    state = synchronize_question(state, proposal)
    assert current_question(state).id != old.id
    rejected = AnswerResolver().resolve("", state, reply=QuestionReply(question_id=old.id, option_id="keep"))
    assert rejected.act == "clarify" and not rejected.actions


@pytest.mark.parametrize("slot,capability,value,expected", [
    ("zone", "window.set_position", "driver", "driver"),
    ("value_celsius", "climate.set_temperature", "24", 24),
    ("location", "weather.get_forecast", "Thành phố Sao Mai", "Thành phố Sao Mai"),
])
def test_typed_slot_values_use_the_same_task_and_preserve_following_step(slot, capability, value, expected):
    state = SessionState(active=DialogueTask(steps=[
        TaskStep(draft={"capability": capability}, status="waiting_slot", missing_slots=[slot]),
        TaskStep(draft={"capability": "media.play"}),
    ]))
    state = synchronize_question(state)
    question = current_question(state)
    answer = AnswerResolver().resolve("", state, reply=QuestionReply(question_id=question.id, value=value))
    assert answer.act == "modify" and getattr(answer.actions[0].arguments, slot) == expected
    updated, error = reduce_turn(state, answer)
    assert error is None and updated.active.id == state.active.id
    assert updated.active.steps[1] == state.active.steps[1]


def test_missing_name_round_trip_completion_keeps_task_and_other_slots():
    state = SessionState(active=DialogueTask(steps=[
        TaskStep(draft={"capability": "trip.plan", "arguments": {"trip_kind": "round_trip"}}, status="waiting_slot"),
        TaskStep(draft={"capability": "media.play"}),
    ]))
    state = synchronize_question(state)
    answer = AnswerResolver().resolve("Bến Tàu Mặt Trăng", state)
    assert answer.act == "modify" and answer.actions[0].arguments.trip_kind == "round_trip"
    updated, error = reduce_turn(state, answer)
    assert error is None and updated.active.id == state.active.id
    assert updated.active.steps[0].draft.arguments.destination.name == "Bến Tàu Mặt Trăng"
    assert updated.active.steps[1] == state.active.steps[1]


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.asyncio
async def test_nearest_category_can_preview_without_extra_place_confirmation(services, tmp_path, structured):
    from tests.test_navigation_workflow import Maps as PharmacyMaps

    maps = PharmacyMaps()
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        request = {"session_id": "driver", "turn_id": "nearest",
                   "input_text": "dẫn tôi đến hiệu thuốc gần nhất", "position": {"lat": 21, "lng": 105.8}}
        if structured:
            request["action"] = {"capability": "navigation.plan", "arguments": {"destination": {
                "selection": "category", "entity_type": "pharmacy", "nearest": True,
            }}}
        result = await graph.ainvoke(request)
        assert result["navigation"]["status"] == "preview"
        assert result["navigation"]["selected"]["name"] == "Nhà thuốc TEST"
        assert result["pending_question"]["purpose"] == "approve_execution"
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_structured_region_keeps_representative_confirmation(services, tmp_path):
    from tests.test_dialogue_audit_oct09 import AuditMaps

    graph, _, gateway = build(replace(services, navigation=AuditMaps(), energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "region", "input_text": "Đến khu vực",
            "action": {"capability": "navigation.plan", "arguments": {"destination": {
                "name": "Đà Nẵng", "selection": "region",
            }}}, "position": {"lat": 21, "lng": 105.8}})
        assert result["pending_question"]["kind"] == "confirm_destination"
        assert result["navigation"]["selected"]["representative"]
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.asyncio
async def test_owned_reference_reuses_candidate_without_search_and_rejects_stale(services, tmp_path, owner):
    maps = Maps("Bến Tàu Mặt Trăng")
    graph, _, gateway = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "search", "input_text": "Tìm địa điểm",
            "action": {"capability": "navigation.search" if owner == "navigation" else "trip.plan",
                       "arguments": {"query": "Bến Tàu Mặt Trăng, Nam Định"}},
            "position": {"lat": 21.01, "lng": 105}})
        handle = first["navigation" if owner == "navigation" else "trip_plan"]["candidates"][0]["handle"]
        calls = len(maps.calls)
        spec = {"selection": "reference", "reference": handle}
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "plan", "input_text": "Dùng điểm vừa tìm",
            "action": {"capability": owner + ".plan", "arguments": {"destination": spec}},
            "position": {"lat": 21.01, "lng": 105}})
        selected = result["navigation" if owner == "navigation" else "trip_plan"]["selected"]
        assert selected and selected["handle"] == handle, (result["status"], result["response_text"], result["steps"])
        assert len(maps.calls) == calls and not gateway.vehicle.state_for("driver").driving
        wrong = await graph.ainvoke({"session_id": "driver", "turn_id": "stale", "input_text": "Điểm cũ",
            "action": {"capability": owner + ".plan", "arguments": {"destination": {**spec, "reference": "unknown"}}}})
        assert wrong["status"] in {"blocked", "clarify"} and len(maps.calls) == calls
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_ui_acceptance_of_task_bound_search_offer_executes_once(services, tmp_path):
    from tests.test_navigation_workflow import Maps as PharmacyMaps

    maps = PharmacyMaps()
    graph, _, gateway = build(replace(services, navigation=maps), tmp_path)
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "offer", "input_text": "tôi đau bụng",
                                     "position": {"lat": 21, "lng": 105.8}})
        question = first["pending_question"]
        assert question["kind"] == "accept_offer" and not maps.queries
        reply = {"question_id": question["id"], "option_id": "yes"}
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "accept", "question_reply": reply,
                                     "position": {"lat": 21, "lng": 105.8}})
        assert result["navigation"]["candidates"] and len(maps.queries) == 1, (result["status"], result["response_text"], result["steps"])
        stale = await graph.ainvoke({"session_id": "driver", "turn_id": "old", "question_reply": reply})
        assert stale["status"] == "clarify" and len(maps.queries) == 1
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.asyncio
async def test_area_answer_retains_question_provenance_in_domain_state(services, tmp_path, owner):
    class ScopedMaps(Maps):
        async def search(self, query, **kwargs):
            if "," not in query:
                self.calls.append(query)
                return []
            return await super().search(query, **kwargs)

    maps = ScopedMaps("Bến Tàu Mặt Trăng")
    graph, _, _ = build(replace(services, navigation=maps), tmp_path)
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "first", "input_text": "Đến điểm mới",
            "action": {"capability": owner + ".plan", "arguments": {"query": "Bến Tàu Mặt Trăng"}},
            "position": {"lat": 21, "lng": 105}})
        question = first["pending_question"]
        assert question["slot"] == "area"
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "area", "input_text": "Bổ sung khu vực",
            "question_reply": {"question_id": question["id"], "value": "Nam Định"}})
        destination = result["navigation" if owner == "navigation" else "trip_plan"]["destination"]
        assert destination["name"] == "Bến Tàu Mặt Trăng"
        assert destination["area"]["source"] == "answer" and destination["area"]["question_id"] == question["id"]
        assert len(maps.calls) == 2
    finally:
        await graph.aclose()


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.asyncio
async def test_typed_missing_destination_can_resume_original_plan(services, tmp_path, owner):
    maps = Maps("Bến Tàu Mặt Trăng")
    graph, _, gateway = build(replace(services, navigation=maps), tmp_path)
    try:
        first = await graph.ainvoke({"session_id": "driver", "turn_id": "missing", "input_text": "Lập kế hoạch",
            "action": {"capability": owner + ".plan", "arguments": {"trip_kind": "round_trip"}},
            "position": {"lat": 21.01, "lng": 105}})
        question = first["pending_question"]
        assert question["slot"] == "name"
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "name", "input_text": "Bổ sung điểm",
            "question_reply": {"question_id": question["id"], "value": "Bến Tàu Mặt Trăng, Nam Định"},
            "position": {"lat": 21.01, "lng": 105}})
        assert result["navigation" if owner == "navigation" else "trip_plan"]["candidates"], result["response_text"]
        assert result["session"]["active"]["id"] == first["session"]["active"]["id"]
        assert result["session"]["active"]["steps"][0]["draft"]["arguments"]["trip_kind"] == "round_trip"
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        await graph.aclose()
