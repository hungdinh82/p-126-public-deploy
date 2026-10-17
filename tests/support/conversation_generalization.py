"""Shared component fixtures extracted from historical scenario modules."""

from src.vivi.dialogue.contracts import (
    DialogueTask,
    NavigationState,
    SessionState,
    TaskStep,
    TripPlanState,
)
from tests.support.long_term_memory import services as services


def pending(owner="navigation", phase="confirm_destination"):
    data = dict(
        status="selecting",
        revision=4,
        request_id="request",
        clarification={
            "phase": phase,
            "region": "Nam Định",
            "revision": 4,
            "question": "Một câu hỏi có wording khác.",
            "choices": [
                {"handle": "keep", "kind": "representative", "label": "Giữ điểm"},
                {"handle": "change", "kind": "specific", "label": "Đổi điểm"},
            ],
        },
    )
    task = DialogueTask(
        steps=[
            TaskStep(draft={"capability": owner + ".plan", "arguments": {"query": "Nam Định"}}, status="waiting_slot")
        ]
    )
    if owner == "trip":
        return SessionState(active=task, trip_plan=TripPlanState(**data), navigation_focus="trip")
    return SessionState(active=task, navigation=NavigationState(**data))
