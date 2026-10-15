"""Rows 898–909: replay both parser and the actual failing model semantics."""

import json
from pathlib import Path

import pytest

from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.reducer import reduce_turn
from tests.support.long_term_memory import services as services

ROWS = [
    json.loads(line)
    for line in (Path(__file__).parents[2] / "tests/fixtures/dialogue_audits/audit_898_909.jsonl")
    .read_text()
    .splitlines()
]


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
