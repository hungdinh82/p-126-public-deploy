"""Log 997–1001: duplicate map records, semantic decision, unresolved start."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.vivi.dialogue.contracts import SemanticTurn, SessionState, TripPlanState
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.presentation import choice_message
from src.vivi.navigation.selection import decide_place, distinct_places
from tests.test_dialogue_v2 import build
from tests.test_long_term_memory import services as services
from tests.test_navigation_refinement import DestinationMaps
from tests.test_navigation_workflow import profile


def station(handle, lng=106.68728, suffix=""):
    return dict(
        handle=handle,
        name="Ga Hải Phòng Phố Lương Khánh Thiện" + suffix,
        address="Phường Gia Viên, Thành phố Hải Phòng",
        lat=20.85616,
        lng=lng,
    )


def test_log_1001_deduplicates_vendor_records_but_not_different_locations():
    candidates = distinct_places([station("a"), station("b", 106.68750, " Lương Khánh Thiện Ngô Quyền Hải Phòng")])
    assert len(candidates) == 1
    assert choice_message(candidates) == "Ga Hải Phòng, Phường Gia Viên. Đúng chỗ này chứ?"
    assert len(distinct_places([station("a"), station("b", 106.70)])) == 2
    assert len(distinct_places([station("a", suffix=" cửa Đông"), station("b", suffix=" cửa Tây")])) == 2


class DecisionModel:
    def __init__(self, reply=None):
        self.reply = reply

    async def agenerate_json(self, **kwargs):
        if kwargs["schema_name"] == "destination_choice":
            if self.reply is not None:
                return json.dumps(self.reply)
            candidate = json.loads(kwargs["user"])["candidates"][0]
            return json.dumps(dict(mode="propose", handle=candidate["handle"], reason="exact_destination"))
        return json.dumps(
            {"act": "request", "actions": [{"capability": "navigation.plan", "arguments": {"query": "Tràng An"}}]}
        )


@pytest.mark.parametrize(
    "reply",
    [
        dict(mode="propose", handle="invented", reason="exact_destination"),
        dict(mode="propose", handle="a", reason="nearest_requested"),
        dict(mode="propose", handle="a", reason="ambiguous"),
        {"garbage": True},
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_judgment_falls_back_to_clarification(reply):
    resolver = SimpleNamespace(providers={"google": SimpleNamespace(client=DecisionModel(reply))})
    result = await decide_place(resolver, "google", "Ga Hải Phòng", [station("a")])
    assert result["mode"] == "clarify" and result["source"] == "fallback"


@pytest.mark.asyncio
async def test_model_proposes_preview_without_extra_place_question_or_driving(services, tmp_path):
    graph, _, gateway = build(
        replace(services, navigation=DestinationMaps(), energy_planner=EnergyPlanner(profile())),
        tmp_path,
        DecisionModel(),
    )
    graph.default_provider = "google"
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "propose",
                "input_text": "đi Tràng An",
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            }
        )
        assert result["status"] == "answered" and result["navigation"]["offer"]["status"] == "pending"
        assert result["navigation"]["selected"]["selection_decision"]["source"] == "google"
        assert "Đúng chỗ này" not in result["response_text"]
        assert not gateway.vehicle.state_for("driver").driving
    finally:
        graph.close()


@pytest.mark.parametrize("text", ["đúng bắt đầu đi luôn", "có"])
def test_logged_start_while_selecting_resolves_unique_place_first(text):
    state = SessionState(trip_plan=TripPlanState(status="selecting", revision=3, candidates=[station("a")]))
    plan = state.trip_plan
    semantic = SemanticTurn.model_validate(
        {
            "act": "request",
            "actions": [
                {"capability": "trip.start", "arguments": {"plan_id": plan.id, "route_revision": plan.revision}}
            ],
        }
    )
    result = OfflineParser().guard(text, semantic, state)
    assert result.actions[0].capability == "trip.select"
    assert result.actions[0].arguments.place_handle == "a"


@pytest.mark.asyncio
async def test_valid_ambiguous_model_decision_keeps_question():
    resolver = SimpleNamespace(
        providers={
            "google": SimpleNamespace(client=DecisionModel(dict(mode="clarify", handle=None, reason="ambiguous")))
        }
    )
    result = await decide_place(resolver, "google", "Ga Hải Phòng", [station("a"), station("b", 106.70)])
    assert result["mode"] == "clarify" and result["source"] == "google"


def test_stale_start_cannot_select_current_plan():
    state = SessionState(trip_plan=TripPlanState(status="selecting", revision=3, candidates=[station("a")]))
    value = SemanticTurn.model_validate(
        {
            "act": "request",
            "actions": [
                {
                    "capability": "trip.start",
                    "arguments": {
                        "plan_id": state.trip_plan.id,
                        "route_revision": 2,
                    },
                }
            ],
        }
    )
    assert OfflineParser().guard("đúng bắt đầu đi luôn", value, state).act == "clarify"
