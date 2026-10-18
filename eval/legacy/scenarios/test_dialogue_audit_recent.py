"""Rows 898–909: replay both parser and the actual failing model semantics."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eval.legacy.grammar import OfflineParser
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.reducer import reduce_turn
from tests.support.dialogue_v2 import ScriptedModel, build, invoke
from tests.support.long_term_memory import services as services

ROWS = [json.loads(line) for line in (Path(__file__).parents[1] / "tests/fixtures/dialogue_audits/audit_898_909.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_recent_logs_replay_wrong_model_semantics(services, tmp_path, tier):
    model = ScriptedModel([r["recorded_semantic"] for r in ROWS])
    graph, _, gateway = build(
        services, tmp_path, model=model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    old_grants = []
    for row in ROWS:
        result = await invoke(graph, row["query"], str(row["row"]))
        bound = any(
            attempt.get("source") in {"pending_confirmation", "candidate_reference", "information_policy"}
            for attempt in result["diagnostics"].get("provider_attempts", [])
        )
        if tier != "rules" and bound:
            # The fixture has one recorded model semantic per source row. Host-
            # owned answers intentionally bypass the model, so keep the replay
            # iterator aligned with the following row.
            next(model.replies)
        assert result["diagnostics"]["effective_provider"] == ("rules" if bound else tier)
        assert result["response_text"].strip()
        if row["row"] == 899:
            assert result["status"] == "clarify"
            assert "đóng" not in result["response_text"]
        if row["row"] == 900:
            assert result["status"] == "clarify" and "trước" in result["response_text"]
        if 901 <= row["row"] <= 904:
            assert result["status"] == "confirmation_required"
            expected = {
                901: ("door.set_open", {"zone": "rear_left", "open": True}),
                902: ("window.set_position", {"zone": "driver", "position_percent": 100}),
                903: ("window.set_position", {"zone": "driver", "position_percent": 100}),
                904: ("window.set_position", {"zone": "front_passenger", "position_percent": 100}),
            }[row["row"]]
            assert result["action_proposal"]["intent"] == expected[0]
            assert result["action_proposal"]["arguments"] == expected[1]
            for grant in old_grants:
                rejected = gateway.resolve_confirmation("driver", f"old-{row['row']}-{grant}", grant, "approve")[1]
                assert rejected.status == "denied" and not rejected.allowed
            old_grants.append(result["confirmation"]["confirmation_id"])
            assert gateway.vehicle.state_for("driver").state_version == 0
        if row["row"] == 905:
            assert len(result["steps"]) == 2 and all(s["status"] == "completed" for s in result["steps"])
            assert gateway.vehicle.state_for("driver").temperature_celsius == 25
            assert gateway.vehicle.state_for("driver").state_version == 2
        if row["row"] >= 906:
            assert all(s["status"] == "completed" for s in result["steps"])
            assert (
                gateway.vehicle.state_for("driver").temperature_celsius
                == {906: 28, 907: 30, 908: 25, 909: 25}[row["row"]]
            )
            assert gateway.vehicle.state_for("driver").media_playing
    graph.close()


def waiting_chain(**temperature):
    return SessionState(
        active=DialogueTask(
            contract_version=6,
            steps=[
                TaskStep(
                    draft=ActionDraft(capability="climate.set_temperature", arguments=temperature),
                    status="waiting_slot",
                ),
                TaskStep(draft=ActionDraft(capability="media.play")),
            ],
        )
    )


@pytest.mark.parametrize(
    "arguments,remaining",
    [
        ({"value_celsius": 25.0}, {"value_celsius": 25.0}),
        ({"temperature_delta_celsius": 3.0}, {"temperature_delta_celsius": 3.0}),
    ],
)
def test_correction_clears_incompatible_old_numeric_mode(arguments, remaining):
    state = waiting_chain(value_celsius=3.0, temperature_delta_celsius=3.0)
    corrected, message = reduce_turn(
        state,
        SemanticTurn(
            act="modify",
            task_ref="active",
            actions=[ActionDraft(capability="climate.set_temperature", arguments=arguments)],
        ),
    )
    assert message is None
    assert corrected.active.steps[0].draft.arguments.model_dump(exclude_none=True) == remaining
    assert corrected.active.revision == state.active.revision + 1


def test_multi_step_correction_is_atomic_and_keeps_ids():
    state = waiting_chain(value_celsius=3.0, temperature_delta_celsius=3.0)
    event = SemanticTurn(
        act="modify",
        task_ref="active",
        actions=[
            ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 25.0}),
            ActionDraft(capability="media.play"),
        ],
    )
    updated, message = reduce_turn(state, event)
    assert message is None
    assert updated.active.id == state.active.id
    assert [s.id for s in updated.active.steps] == [s.id for s in state.active.steps]
    assert updated.active.revision == state.active.revision + 1
    assert updated.active.steps[0].draft.arguments.model_dump(exclude_none=True) == {"value_celsius": 25.0}
    event.actions[1] = ActionDraft(capability="window.set_position", arguments={"zone": "driver"})
    unchanged, message = reduce_turn(state, event)
    assert message
    assert unchanged.active.model_dump(exclude={"expires_at"}) == state.active.model_dump(exclude={"expires_at"})


@pytest.mark.parametrize(
    "text,delta",
    [
        ("tăng lên 3 độ", 3.0),
        ("giảm xuống 2 độ C", -2.0),
        ("tăng thêm 3 độ C ấy", 3.0),
        ("hạ bớt 1 độ", -1.0),
    ],
)
def test_relative_temperature_grammar_and_model_contradiction(text, delta):
    state = waiting_chain()
    parser = OfflineParser()
    model = SemanticTurn(
        act="modify",
        task_ref="active",
        actions=[ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": abs(delta)})],
    )
    corrected = parser.guard(text, model, state)
    assert corrected.actions[0].arguments.model_dump(exclude_none=True) == {"temperature_delta_celsius": delta}


@pytest.mark.asyncio
async def test_multi_correction_revokes_grant_and_never_replays_completed_step(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "mở cửa bên tài và bật nhạc", "one")
    corrected = await invoke(graph, "không, cửa sổ phía sau và bật nhạc", "two")
    assert corrected["status"] == "confirmation_required"
    assert corrected["action_proposal"]["arguments"] == {"zone": "rear_left", "position_percent": 100}
    denied = gateway.resolve_confirmation("driver", "old", first["confirmation"]["confirmation_id"], "approve")[1]
    assert not denied.allowed
    assert gateway.vehicle.state_for("driver").state_version == 0
    approved = await invoke(graph, "đồng ý", "three")
    assert all(s["status"] == "completed" for s in approved["steps"])
    assert gateway.vehicle.state_for("driver").state_version == 2
    assert not gateway.vehicle.state_for("driver").door_states.driver.open
    assert gateway.vehicle.state_for("driver").window_positions.rear_left == 100
    replay = await invoke(graph, "đồng ý", "three")
    assert replay["steps"] == approved["steps"] and gateway.vehicle.state_for("driver").state_version == 2
    graph.close()


@pytest.mark.asyncio
async def test_rejected_compound_correction_revokes_old_consent(services, tmp_path):
    model = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [{"capability": "door.set_open", "arguments": {"zone": "driver", "open": True}}],
            },
            {
                "act": "modify",
                "task_ref": "active",
                "actions": [
                    {"capability": "window.set_position", "arguments": {"zone": "rear_left"}},
                    {"capability": "media.play"},
                ],
            },
            {"act": "confirm", "task_ref": "active"},
        ]
    )
    graph, _, gateway = build(services, tmp_path, model=model)
    first = await invoke(graph, "mở cửa bên tài", "one")
    rejected = await invoke(graph, "thay thao tác đầu, bổ sung thao tác cuối", "two")
    assert rejected["status"] == "clarify"
    assert rejected["session"]["active"]["revision"] > first["session"]["active"]["revision"]
    old = gateway.resolve_confirmation("driver", "old", first["confirmation"]["confirmation_id"], "approve")[1]
    assert not old.allowed
    await invoke(graph, "đồng ý", "three")
    assert gateway.vehicle.state_for("driver").state_version == 0
    graph.close()


@pytest.mark.parametrize("query", ["25 độ và phát nhạc", "đặt nhiệt độ 25 và bật nhạc"])
@pytest.mark.asyncio
async def test_compound_slot_answer_retains_original_task(services, tmp_path, query):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "đặt nhiệt độ và bật nhạc", "one")
    result = await invoke(graph, query, "two")
    assert result["session"]["active"]["id"] == first["session"]["active"]["id"]
    assert [s["id"] for s in result["steps"]] == [s["id"] for s in first["steps"]]
    assert all(s["status"] == "completed" for s in result["steps"])
    assert gateway.vehicle.state_for("driver").temperature_celsius == 25
    assert gateway.vehicle.state_for("driver").media_playing
    replay = await invoke(graph, query, "two")
    assert replay["steps"] == result["steps"]
    assert gateway.vehicle.state_for("driver").state_version == 2
    graph.close()


@pytest.mark.asyncio
async def test_persisted_bad_temperature_can_be_repaired_after_restart(services, tmp_path):
    graph, store, gateway = build(services, tmp_path)
    await invoke(graph, "đặt nhiệt độ và bật nhạc", "one")
    config = graph.config(graph.event({"session_id": "driver", "turn_id": "one", "input_text": "x"}))
    snapshot = await graph.compiled.aget_state(config)
    state = SessionState.model_validate(snapshot.values["session"])
    state.active.steps[0].draft.arguments.value_celsius = 3.0
    state.active.steps[0].draft.arguments.temperature_delta_celsius = 3.0
    state.active.expires_at = datetime.now(UTC) + timedelta(minutes=3)
    await graph.compiled.aupdate_state(config, {"session": state.model_dump(mode="json")}, as_node="persist")
    graph.close()
    graph, _, _ = build(services, tmp_path, store=store)
    result = await invoke(graph, "đặt nhiệt độ 25", "repair")
    assert all(s["status"] == "completed" for s in result["steps"])
    assert gateway.vehicle.state_for("driver").temperature_celsius == 25
    assert gateway.vehicle.state_for("driver").media_playing
    graph.close()
