"""Independent destination decisions: ambiguity, rejection, correction and consent."""

from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import NavigationState, SessionState, TripPlanState
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.navigation.energy import EnergyPlanner
from tests.test_dialogue_v2 import ScriptedModel, build, invoke
from tests.test_long_term_memory import services as services
from tests.test_navigation_refinement import DestinationMaps
from tests.test_navigation_workflow import profile


def selection(owner, count=2, same_name=False):
    data = dict(
        status="selecting",
        revision=3,
        candidates=[
            {"handle": f"place:{i}", "name": "Tràng An" if same_name else f"Địa điểm {i}"} for i in range(count)
        ],
    )
    if owner == "trip":
        return SessionState(trip_plan=TripPlanState(**data))
    return SessionState(navigation=NavigationState(**data))


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.parametrize(
    "utterance,count,capability",
    [
        ("không", 1, "refine"),
        ("không đúng chỗ này", 1, "refine"),
        ("không chỗ nào", 2, "refine"),
        ("đổi sang Tràng An", 2, "refine"),
        ("chỗ thứ hai", 2, "select"),
        ("số 2", 2, "select"),
        ("chọn 2", 2, "select"),
        ("đến đó", 1, "select"),
        ("đến đó", 2, None),
        ("chỗ thứ ba", 2, None),
        ("chỗ thứ 0", 2, None),
        ("chỗ thứ 9", 2, None),
        ("đồng ý", 2, None),
        ("đúng", 1, "select"),
    ],
)
def test_candidate_answer_matrix(owner, utterance, count, capability):
    result = OfflineParser().interpret(utterance, selection(owner, count))
    if capability is None:
        assert result.act == "clarify" and not result.actions
    else:
        assert result.actions[0].capability == f"{owner}.{capability}"
        if utterance.startswith("không"):
            assert result.actions[0].arguments.reject_candidates is True
        if utterance.startswith("đổi"):
            assert result.actions[0].arguments.query == "Tràng An"
    assert not any(action.capability.endswith(".start") for action in result.actions)


@pytest.mark.parametrize("owner", ["navigation", "trip"])
def test_duplicate_name_requires_disambiguation(owner):
    result = OfflineParser().interpret("Tràng An", selection(owner, same_name=True))
    assert result.act == "clarify" and not result.actions


@pytest.mark.parametrize("owner", ["navigation", "trip"])
@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_reject_then_new_name_keeps_chain_and_never_starts(services, tmp_path, owner, tier):
    maps = DestinationMaps()
    model = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [
                    {"capability": f"{owner}.plan", "arguments": {"query": "Tràng An", "trip_kind": "one_way"}},
                    {"capability": "media.play", "arguments": {}},
                ],
            }
        ]
    )
    graph, _, gateway = build(
        replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())),
        tmp_path,
        model if tier == "google" else None,
        local=model if tier == "local" else None,
    )
    graph.default_provider = tier
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        initial = "đi Tràng An rồi bật nhạc" if owner == "navigation" else "lập lộ trình Tràng An rồi bật nhạc"
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "choose",
                "input_text": initial,
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            }
        )
        assert len(first["steps"]) == 2

        def mock_reply(result, arguments):
            state = result["trip_plan" if owner == "trip" else "navigation"]
            if owner == "trip":
                arguments.update(plan_id=state["id"], route_revision=state["revision"])
            model.replies = iter(
                [{"act": "request", "actions": [{"capability": f"{owner}.refine", "arguments": arguments}]}]
            )

        mock_reply(first, {"reject_candidates": True})
        rejected = await invoke(graph, "không đúng chỗ này", "reject")
        state = rejected["trip_plan" if owner == "trip" else "navigation"]
        assert state["clarification"]["phase"] == "specific"
        assert state["candidates"] == []
        assert len(rejected["steps"]) == 2
        mock_reply(rejected, {"query": "Tràng An"})
        again = await invoke(graph, "Tràng An", "new-name")
        assert len(again["steps"]) == 2
        assert maps.queries[-1][0] == "Tràng An"
        assert not gateway.vehicle.state_for("driver").driving
        assert not gateway.vehicle.state_for("driver").media_playing
    finally:
        graph.close()
