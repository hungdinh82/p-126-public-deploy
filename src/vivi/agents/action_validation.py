from __future__ import annotations

from src.vivi.agents.contracts import ActionProposal, IntentDecision
from src.vivi.agents.tools import validate_tool_arguments


def validate_action_arguments(decision: IntentDecision) -> tuple[ActionProposal | None, str | None]:
    """Apply exactly the same tool schema to model, rules and saved commands."""
    arguments, error = validate_tool_arguments(decision.intent, decision.arguments.model_dump(exclude_none=True))
    if error:
        return None, error
    return ActionProposal(intent=decision.intent, arguments=arguments, confidence=decision.confidence), None
