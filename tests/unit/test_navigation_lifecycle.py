"""Parking, continuing, replacing and clearing server-owned journeys."""

from tests.support.long_term_memory import services as services
from tests.support.navigation_lifecycle import event as event
from tests.support.navigation_lifecycle import runtime as runtime
from tests.support.navigation_lifecycle import start as start


def test_clear_removes_completed_journey_context_but_preserves_other_capabilities():
    from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SessionState, TaskStep, TripPlanState
    from src.vivi.dialogue.reducer import clear_journey

    session = SessionState(
        active=DialogueTask(
            steps=[
                TaskStep(draft=ActionDraft(capability="media.play"), status="completed"),
                TaskStep(
                    draft=ActionDraft(capability="navigation.start", arguments={"route_revision": 1}),
                    status="completed",
                    result={"navigation_update": {"route": "old"}},
                ),
            ]
        ),
        suspended=[
            DialogueTask(
                steps=[
                    TaskStep(
                        draft=ActionDraft(capability="navigation.plan", arguments={"query": "Vinhomes"}),
                        status="waiting_slot",
                    )
                ]
            )
        ],
        trip_plan=TripPlanState(),
    )
    cleared = clear_journey(session)
    assert [step.draft.capability for step in cleared.active.steps] == ["media.play"]
    assert cleared.active.steps[0].status == "completed"
    assert not cleared.suspended
    assert cleared.navigation_workspace == session.navigation_workspace  # Domain clearing belongs to its writer.
