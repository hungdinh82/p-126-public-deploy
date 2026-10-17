"""Latest logs, defaults and model-owned task patches under the same policy."""

import pytest
from pydantic import ValidationError

from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.reducer import reduce_turn
from tests.support.long_term_memory import services as services


def test_invalid_patch_does_not_mutate_any_slot_and_revokes_consent():
    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(
                    draft=ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 24}),
                    status="completed",
                ),
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position", arguments={"zone": "driver", "position_percent": 100}
                    ),
                    status="waiting_confirmation",
                    confirmation={"id": "old"},
                ),
            ]
        )
    )
    event = SemanticTurn(
        act="modify",
        task_ref="active",
        step_updates=[
            {"step_index": 1, "action": {"capability": "window.set_position", "arguments": {"position_percent": 50}}},
            {"step_index": 0, "action": {"capability": "climate.set_temperature", "arguments": {"value_celsius": 26}}},
        ],
    )
    result, message = reduce_turn(state, event)
    assert message and result.active.steps[1].draft.arguments.position_percent == 100
    assert result.active.steps[0].status == "completed" and not result.active.steps[1].confirmation
    assert result.active.revision > state.active.revision


def test_patch_duplicates_and_more_than_five_steps_are_rejected():
    with pytest.raises(ValidationError):
        SemanticTurn(
            act="modify",
            task_ref="active",
            step_updates=[
                {"step_index": 0, "action": {"capability": "media.play"}},
                {"step_index": 0, "action": {"capability": "media.pause"}},
            ],
        )
    state = SessionState(
        active=DialogueTask(steps=[TaskStep(draft=ActionDraft(capability="media.play")) for _ in range(5)])
    )
    result, message = reduce_turn(
        state, SemanticTurn(act="modify", task_ref="active", append_actions=[ActionDraft(capability="media.pause")])
    )
    assert message and len(result.active.steps) == 5


def test_model_context_supplies_exclusions_and_editable_step_handles():
    from src.vivi.dialogue.planner import SemanticResolver

    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(draft=ActionDraft(capability="media.play"), status="completed"),
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position",
                        arguments={"zone": "all", "position_percent": 100},
                        excluded_zones=["driver"],
                    ),
                    status="waiting_confirmation",
                ),
            ]
        )
    )
    context = SemanticResolver.context(state)["active"]
    assert context["editable_steps"] == [1] and context["current_step"] == 1
    assert context["steps"][1]["step_handle"] == "active:step:1"
    assert context["steps"][1]["excluded_zones"] == ["driver"]


def test_explicit_replacement_of_exclusions_requires_replace_mode():
    state = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(
                    draft=ActionDraft(
                        capability="window.set_position",
                        arguments={"zone": "all", "position_percent": 100},
                        excluded_zones=["driver"],
                    )
                )
            ]
        )
    )
    result, message = reduce_turn(
        state,
        SemanticTurn(
            act="modify",
            task_ref="active",
            step_updates=[
                {
                    "step_index": 0,
                    "action": {
                        "capability": "window.set_position",
                        "excluded_zones": ["front_passenger"],
                        "exclusion_mode": "replace",
                    },
                },
            ],
        ),
    )
    assert message is None and result.active.steps[0].draft.excluded_zones == ["front_passenger"]
