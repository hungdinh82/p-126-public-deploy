"""Episode-level regressions: actual tools and persisted state, not wording alone."""

from src.vivi.dialogue.contracts import SemanticTurn, SessionState
from src.vivi.dialogue.reducer import reduce_turn
from tests.support.long_term_memory import services as _services

services = _services


def test_invalid_offer_cannot_execute_without_validation():
    from src.vivi.capabilities.registry import REGISTRY
    from src.vivi.dialogue.contracts import ActionDraft

    state, _ = reduce_turn(
        SessionState(),
        SemanticTurn(act="offer", actions=[ActionDraft(capability="door.set_open", arguments={"open": True})]),
    )
    state, _ = reduce_turn(state, SemanticTurn(act="confirm", task_ref="active"))
    assert REGISTRY["door.set_open"].validate(state.active.steps[0].draft)[1]
