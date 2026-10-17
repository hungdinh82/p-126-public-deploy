"""Navigation domain contracts, independent of dialogue and graph state."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from src.vivi.navigation.contracts import DestinationSpec


class RouteSnapshot(BaseModel):
    """An immutable-by-convention calculated route, shared by plans and journeys."""

    request_id: str = ""
    destination: DestinationSpec | None = None
    route_id: str | None = None
    revision: int = 0
    event_sequence: int = 0
    trip_kind: Literal["one_way", "round_trip"] | None = None
    waypoint_routes: list[dict] = Field(default_factory=list)
    origin: dict | None = None
    summary: dict | None = None
    selected: dict | None = None
    route: dict | None = None
    energy: dict | None = None
    alternatives: list[dict] = Field(default_factory=list)
    initial_soc: float | None = None


class PlanDraft(RouteSnapshot):
    id: str = Field(default_factory=lambda: "plan:" + str(uuid4()))
    purpose: Literal["navigate", "itinerary"] = "navigate"
    calculation_status: Literal["draft", "ready", "incomplete", "cancelled"] = "draft"
    started_revision: int | None = None
    calculation_basis: str | None = None
    status: Literal["idle", "selecting", "preview"] = "idle"
    candidates: list[dict] = Field(default_factory=list)
    selection_decision: dict | None = None
    clarification: dict | None = None
    offer: dict | None = None
    phase: Literal["needs_origin", "needs_destination", "ready", "incomplete"] = "needs_destination"


class ActiveJourney(RouteSnapshot):
    status: Literal["running", "charging", "paused", "completed", "ended"]
    resume_status: Literal["running", "charging"] | None = None
    progress_m: float = 0
    last_assessment_m: float = 0
    last_assessment_soc: float | None = None
    alerts: list[dict] = Field(default_factory=list)
    last_alert_at: datetime | None = None
    last_alert_severity: Literal["warning", "critical"] | None = None
    simulated_soc: float | None = None
    stop_index: int = 0
    charge_elapsed_s: float = 0


class NavigationState(BaseModel):
    request_id: str = ""
    destination: DestinationSpec | None = None
    route_id: str | None = None
    revision: int = 0
    event_sequence: int = 0
    status: Literal["idle", "selecting", "preview", "running", "charging", "paused", "completed", "ended"] = "idle"
    resume_status: Literal["running", "charging"] | None = None
    trip_kind: Literal["one_way", "round_trip"] | None = None
    waypoint_routes: list[dict] = Field(default_factory=list)
    origin: dict | None = None
    candidates: list[dict] = Field(default_factory=list)
    selection_decision: dict | None = None
    clarification: dict | None = None
    summary: dict | None = None
    selected: dict | None = None
    route: dict | None = None
    energy: dict | None = None
    alternatives: list[dict] = Field(default_factory=list)
    progress_m: float = 0
    initial_soc: float | None = None
    last_assessment_m: float = 0
    last_assessment_soc: float | None = None
    alerts: list[dict] = Field(default_factory=list)
    last_alert_at: datetime | None = None
    last_alert_severity: Literal["warning", "critical"] | None = None
    simulated_soc: float | None = None
    stop_index: int = 0
    charge_elapsed_s: float = 0
    pending: dict | None = None
    offer: dict | None = None
    applied_events: list[str] = Field(default_factory=list)


class NavigationWorkspace(BaseModel):
    """Navigation-owned state. Flat snapshots are transient calculation DTOs."""

    version: int = 1
    revision: int = 0
    cleared_at_revision: int = 0
    event_sequence: int = 0
    origin: dict | None = None
    draft: PlanDraft | None = None
    journey: ActiveJourney | None = None
    history: list[dict] = Field(default_factory=list)
    pending_status: str | None = None
    pending_message: str = ""
    applied_events: list[str] = Field(default_factory=list)

    @classmethod
    def from_snapshot(cls, nav, previous=None):
        data = nav.model_dump(mode="json")
        live = nav.status in {"running", "charging", "paused", "completed", "ended"}
        journey = ActiveJourney.model_validate(data) if live and nav.route else None
        if journey and previous and previous.journey and previous.journey.route_id == journey.route_id:
            journey.revision = previous.journey.revision
        pending = nav.pending or {}
        draft_data = pending.get("navigation") if live else data
        if live and not draft_data and (nav.clarification or nav.candidates):
            draft_data = {
                "status": "selecting",
                "request_id": nav.request_id,
                "revision": nav.revision,
                "origin": nav.origin,
                "candidates": nav.candidates,
                "clarification": nav.clarification,
                "destination": (nav.clarification or {}).get("destination"),
            }
        draft = None
        if draft_data and (
            draft_data.get("route")
            or draft_data.get("candidates")
            or draft_data.get("clarification")
            or draft_data.get("status") == "selecting"
        ):
            if previous and previous.draft:
                draft_data = {**previous.draft.model_dump(mode="json"), **draft_data}
            draft = PlanDraft.model_validate(draft_data)
            draft.phase = (
                "needs_origin"
                if not draft.origin
                else "needs_destination"
                if draft.clarification or not draft.route
                else "ready"
                if (draft.energy or {}).get("selected_plan")
                else "incomplete"
            )
            if not draft.offer and previous and previous.draft and previous.draft.revision == draft.revision:
                draft.offer = previous.draft.offer
        return cls(
            revision=nav.revision,
            cleared_at_revision=previous.cleared_at_revision if previous else 0,
            event_sequence=nav.event_sequence,
            origin=nav.origin,
            history=list(previous.history) if previous else [],
            draft=draft,
            journey=journey,
            pending_status=pending.get("status"),
            pending_message=pending.get("message", ""),
            applied_events=nav.applied_events,
        )

    def snapshot(self):
        base = self.journey or self.draft
        nav = NavigationState.model_validate(base.model_dump(mode="json") if base else {"origin": self.origin})
        nav.revision, nav.event_sequence = self.revision, self.event_sequence
        nav.applied_events = list(self.applied_events)
        if self.journey and self.draft:
            nav.request_id = self.draft.request_id
            nav.candidates = list(self.draft.candidates)
            nav.clarification = self.draft.clarification
            nav.pending = {
                "status": self.pending_status
                or ("selecting" if self.draft.clarification or not self.draft.route else "preview"),
                "message": self.pending_message,
                "navigation": NavigationState.model_validate(self.draft.model_dump(mode="json")).model_dump(
                    mode="json"
                ),
            }
        elif self.pending_status:
            nav.pending = {"status": self.pending_status, "message": self.pending_message}
        return nav


class TripPlanState(PlanDraft):
    """A calculation: no progress, gear, charging timer or pause lifecycle."""

    id: str = Field(default_factory=lambda: "trip:" + str(uuid4()))
    purpose: Literal["itinerary"] = "itinerary"
    calculation_status: Literal["draft", "ready", "incomplete", "cancelled"] = "draft"
    started_revision: int | None = None
    calculation_basis: str | None = None

    @property
    def pending(self):
        return None


class NavigationDomain(BaseModel):
    """The only persisted navigation aggregate; no tasks or dialogue questions."""

    control_revision: int = 0
    snapshot_sequence: int = 0
    control_receipts: list[dict] = Field(default_factory=list)
    navigation_workspace: NavigationWorkspace = Field(default_factory=NavigationWorkspace)
    navigation_focus: Literal["navigation", "trip"] = "navigation"

    @property
    def navigation(self):
        return self.navigation_workspace.snapshot()

    @navigation.setter
    def navigation(self, value):
        self.navigation_workspace = NavigationWorkspace.from_snapshot(value, self.navigation_workspace)

    @property
    def trip_plan(self):
        draft = self.navigation_workspace.draft
        return draft if draft and draft.purpose == "itinerary" else None

    @trip_plan.setter
    def trip_plan(self, value):
        if value is not None:
            self.navigation_workspace.draft = PlanDraft.model_validate(
                {**value.model_dump(mode="json"), "purpose": "itinerary"}
            )
        elif self.trip_plan:
            self.navigation_workspace.draft = None


def record_navigation(session, update):
    state = session.model_copy(deep=True)
    nav = NavigationState.model_validate(update)
    workspace = state.navigation_workspace
    if nav.route and nav.status in {"running", "charging", "paused", "completed", "ended"}:
        previous = workspace.journey
        workspace.journey = ActiveJourney.model_validate(update)
        if previous and previous.route_id == nav.route_id:
            workspace.journey.revision = previous.revision
        if nav.pending and nav.pending.get("navigation"):
            workspace.draft = PlanDraft.model_validate(nav.pending["navigation"])
        if nav.status in {"completed", "ended"} and (not previous or previous.status != nav.status):
            workspace.history = [
                *workspace.history[-19:],
                {"id": nav.route_id, "destination": nav.selected, "status": nav.status, "distance_m": nav.progress_m},
            ]
    else:
        old = workspace.draft.model_dump(mode="json") if workspace.draft else {}
        workspace.draft = PlanDraft.model_validate({**old, **update})
    workspace.event_sequence += 1
    workspace.revision = max(workspace.revision, nav.revision)
    workspace.origin = nav.origin or workspace.origin
    return state


def record_trip_plan(session, update):
    state = session.model_copy(deep=True)
    state.navigation_workspace.draft = PlanDraft.model_validate({**update, "purpose": "itinerary"})
    state.navigation_focus = "trip"
    return state


def clear_navigation(session):
    state = session.model_copy(deep=True)
    workspace = state.navigation_workspace
    state.navigation_workspace = NavigationWorkspace(
        origin=workspace.origin,
        revision=workspace.revision + 1,
        cleared_at_revision=workspace.revision + 1,
        history=workspace.history,
    )
    return state
