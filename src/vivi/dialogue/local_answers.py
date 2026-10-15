"""Resolve short local slot answers without changing the cloud/main resolver."""
from datetime import UTC, datetime

from src.vivi.dialogue.contracts import SemanticTurn
from src.vivi.dialogue.parser import Lexer, OfflineParser
from src.vivi.dialogue.questions import current_question


def resolve_location_component(text, session):
    question = current_question(session)
    if (not question or question.expires_at <= datetime.now(UTC)
        or question.purpose != 'collect_slot' or question.slot not in {None, 'zone', 'target_side', 'target_row'} or not question.slot_effect):
        return None
    draft = question.slot_effect
    if draft.capability not in {'door.set_open', 'door.set_lock', 'window.set_position', 'seat.set_heat_level'}:
        return None
    patch = OfflineParser().slot_patch(Lexer().normalize(text), draft, text)
    if not patch or patch.capability != draft.capability:
        return None
    changed = patch.arguments.model_dump(exclude_none=True)
    if not changed or set(changed) - {'target_side', 'target_row'}:
        return None
    arguments = draft.arguments.model_dump(exclude_none=True)
    arguments.pop('zone', None)
    arguments.pop('zones', None)
    arguments.update(changed)
    effect = draft.model_copy(deep=True)
    effect.arguments = type(effect.arguments).model_validate(arguments)
    return SemanticTurn(act='modify', task_ref='active', actions=[effect])
