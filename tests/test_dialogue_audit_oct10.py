"""Recent navigation/task regressions: real effects always stay simulated."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.vivi.dialogue.contracts import ActionDraft
from src.vivi.navigation.energy import EnergyPlanner, EnergyProfile, load_stations
from src.vivi.navigation.vietmap import Place, Route
from tests.test_dialogue_v2 import ScriptedModel, build, invoke
from tests.test_long_term_memory import services as services
from tests.test_navigation_workflow import HANOI, IntercityMaps

ROOT = Path(__file__).parents[1]
AUDIT = {r["row"]: r for r in map(json.loads, (ROOT / "eval/audit_925_958.jsonl").read_text().splitlines())}


class NearbyMaps:
    def __init__(self, count=3):
        self.count, self.queries = count, []

    async def search(self, text, **kwargs):
        self.queries.append(text)
        return [
            Place(ref_id=str(i), name=f"Quán phở TEST {i + 1}", address="Địa chỉ mô phỏng", lat=21, lng=105.9 + i / 100)
            for i in range(self.count)
        ]

    async def route(self, a, b):
        return Route(distance_m=5000, duration_s=600, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


async def located(graph, text, turn, **extra):
    return await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": turn,
            "input_text": text,
            "position": {"lat": 21, "lng": 105.8, "simulated": True},
            **extra,
        }
    )


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.parametrize("greeting_reply", [True, False])
@pytest.mark.asyncio
async def test_greeting_comfort_and_dislike_never_invent_climate_setting(services, tmp_path, tier, greeting_reply):
    model = ScriptedModel(
        [
            {"act": "reply", "text": "Mình đây, bạn cần gì thế?"}
            if greeting_reply
            else AUDIT[925]["recorded_semantic"],
            AUDIT[928]["recorded_semantic"],
        ]
    )
    graph, _, vehicle = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    first = await invoke(graph, AUDIT[925]["query"], "comfort")
    assert first["execution"]["verified"] and vehicle.vehicle.state_for("driver").temperature_celsius == 21
    second = await invoke(graph, AUDIT[928]["query"], "dislike")
    assert not (second.get("execution") or {}).get("executed")
    assert vehicle.vehicle.state_for("driver").temperature_celsius == 21
    graph.close()


@pytest.mark.parametrize("row", [942, 946, 948])
@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_search_setting_directions_preserves_order_and_voice_choice(services, tmp_path, row, tier):
    maps = NearbyMaps()
    services = replace(services, navigation=maps)
    model = ScriptedModel([AUDIT[row]["recorded_semantic"]])
    graph, _, gateway = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = "rules"
    # An unrelated old preview must never run before a fresh complete request.
    await located(graph, "chỉ đường tới địa điểm cũ", "old-search")
    await invoke(graph, "chỗ đầu tiên", "old-choice")
    graph.default_provider = tier
    # The seed uses parser so the captured model output belongs to the new turn.
    if tier != "rules":
        model.replies = iter([AUDIT[row]["recorded_semantic"]])
    first = await located(graph, AUDIT[row]["query"], "multi")
    assert [s["capability"] for s in first["steps"]] == [
        "navigation.search",
        "climate.set_temperature",
        "navigation.plan",
    ]
    assert [s["status"] for s in first["steps"]] == ["completed", "completed", "waiting_slot"]
    assert gateway.vehicle.state_for("driver").temperature_celsius == 24
    assert not gateway.vehicle.state_for("driver").driving
    assert first["navigation"]["status"] == "selecting" and first["navigation"]["route"] is None
    assert len(maps.queries) == 2  # Reuse search results; do not search again for bare directions.
    task_id = first["session"]["active"]["id"]
    graph.default_provider = "rules"
    chosen = await invoke(graph, "chỗ thứ hai", "choice")
    assert chosen["session"]["active"]["id"] == task_id
    assert len(chosen["steps"]) == 3 and chosen["status"] == "confirmation_required"
    assert chosen["navigation"]["selected"]["name"].endswith("2")
    started = await invoke(graph, "đồng ý", "start")
    assert all(s["status"] == "completed" for s in started["steps"])
    replay = await invoke(graph, "đồng ý", "start")
    assert replay["steps"] == started["steps"]
    graph.close()


@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.asyncio
async def test_selection_keeps_pending_steps_and_early_start_does_not_replace_task(services, tmp_path, typed):
    services = replace(services, navigation=NearbyMaps(1))
    model = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [
                    {"capability": "navigation.plan", "arguments": {"query": "quán phở"}},
                    {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}},
                    {"capability": "media.play"},
                ],
            }
        ]
    )
    graph, _, gateway = build(services, tmp_path, model=model)
    first = await located(graph, "dẫn tới quán phở rồi chỉnh 24 độ và bật nhạc", "multi")
    assert first["navigation"]["status"] == "selecting" and not first.get("confirmation")
    before_ids = [s["id"] for s in first["steps"]]
    early = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "early",
            "action": {
                "capability": "navigation.start",
                "arguments": {"route_revision": first["navigation"]["revision"]},
            },
        }
    )
    assert [s["id"] for s in early["steps"]] == before_ids
    graph.default_provider = "rules"
    if typed:
        chosen = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "choice",
                "action": {
                    "capability": "navigation.select",
                    "arguments": {"place_handle": first["navigation"]["candidates"][0]["handle"]},
                },
            }
        )
    else:
        chosen = await invoke(graph, "đồng ý", "choice")  # Unique place acceptance, not route acceptance.
    assert [s["id"] for s in chosen["steps"]] == before_ids
    assert chosen["status"] == "confirmation_required" and not gateway.vehicle.state_for("driver").driving
    started = await invoke(graph, "đồng ý", "start")
    assert all(s["status"] == "completed" for s in started["steps"])
    assert (
        gateway.vehicle.state_for("driver").temperature_celsius == 24
        and gateway.vehicle.state_for("driver").media_playing
    )
    graph.close()


@pytest.mark.asyncio
async def test_decline_hood_and_close_trunk_does_not_open_hood(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    await invoke(graph, "mở cốp và mở nắp capo", "open")
    await invoke(graph, "ok", "trunk")
    closed = await invoke(graph, "không, đóng cốp luôn đi", "close")
    assert closed["status"] == "confirmation_required"
    assert closed["action_proposal"]["intent"] == "trunk.set_open"
    assert closed["action_proposal"]["arguments"]["open"] is False
    closed = await invoke(graph, "đồng ý", "close-confirm")
    state = gateway.vehicle.state_for("driver")
    assert not state.trunk_open and not state.hood_open
    assert closed["execution"]["verified"]
    graph.close()


@pytest.mark.parametrize("soc,stops", [(70, 0), (40, 1)])
@pytest.mark.asyncio
async def test_data_vf8_plus_profile_hand_calculation_and_charging_e2e(services, tmp_path, soc, stops):
    profile = EnergyProfile.model_validate_json((ROOT / "data/energy/vf8-plus.json").read_text())
    assert profile.simulated and profile.edition.startswith("Plus")
    stations = load_stations(ROOT / "data/energy/charging-stations.simulated.json")
    services = replace(services, navigation=IntercityMaps(), energy_planner=EnergyPlanner(profile, stations))
    services.action_gateway.vehicle.set_energy("driver", soc, 300)
    graph, _, _ = build(services, tmp_path)
    first = await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": "plan",
            "input_text": "lập lộ trình tới Hải Phòng",
            "position": {"lat": HANOI[0], "lng": HANOI[1], "simulated": True},
        }
    )
    assert first["navigation"]["status"] == "selecting"
    chosen = await invoke(graph, "chỗ đầu tiên", "choose")
    plan = chosen["navigation"]["energy"]["selected_plan"]
    assert len(plan["stops"]) == stops and plan["simulated"]
    if not stops:
        assert plan["arrival_soc"] == pytest.approx(70 - 30.36 / 87.7 * 100, abs=0.01)
    else:
        assert plan["stops"][0]["arrival_soc"] == pytest.approx(40 - 12.65 / 87.7 * 100, abs=0.01)
        assert plan["arrival_soc"] >= 15 and plan["stops"][0]["charge_minutes"] > 5
    started = await invoke(graph, "đồng ý", "start")
    assert started["navigation"]["status"] == "running"
    graph.close()


def test_plan_missing_destination_does_not_fabricate_previous_result():
    from src.vivi.capabilities.registry import REGISTRY

    args, message = REGISTRY["navigation.plan"].validate(ActionDraft(capability="navigation.plan"))
    assert args is None and message
    assert load_stations(ROOT / "data/energy/charging-stations.json") == []


def test_upgrade_revokes_auto_selected_route_but_preserves_completed_effects():
    from src.vivi.dialogue.contracts import DialogueTask, NavigationState, SessionState, TaskStep
    from src.vivi.dialogue.reducer import upgrade_contract

    state = SessionState(
        active=DialogueTask(
            contract_version=7,
            steps=[
                TaskStep(
                    draft=ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 24}),
                    status="completed",
                    command_id="already-executed",
                ),
                TaskStep(
                    draft=ActionDraft(capability="navigation.plan", arguments={"query": "quán phở"}),
                    status="waiting_confirmation",
                    confirmation={"confirmation_id": "old"},
                ),
            ],
        ),
        navigation=NavigationState(status="preview", selected={"name": "Auto-selected"}, route={"distance_m": 5000}),
    )
    changed = upgrade_contract(state)
    assert changed.active.contract_version == 9
    assert changed.active.steps[0].status == "completed" and changed.active.steps[0].command_id == "already-executed"
    assert changed.active.steps[1].confirmation is None and changed.active.steps[1].status == "blocked"
    assert changed.active.steps[1].result["error_code"] == "trip_kind_required"
    assert changed.navigation.trip_kind is None
    assert upgrade_contract(changed) == changed
