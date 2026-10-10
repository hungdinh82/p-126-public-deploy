"""Semantic proposals and checkpoint-owned dialogue state.

Providers describe requests; only the reducer owns task identities and revisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, model_validator


class Arguments(BaseModel):
    """Nullable capability slots; schemas in the registry validate execution."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    value_celsius: StrictFloat | None = None
    position_percent: StrictInt | None = None
    open: StrictBool | None = None
    locked: StrictBool | None = None
    level: StrictInt | None = None
    zone: Literal["driver", "front_passenger", "rear_left", "rear_right", "all"] | None = None
    zones: list[Literal["driver", "front_passenger", "rear_left", "rear_right"]] | None = None
    media_query: str | None = None
    memory_key: str | None = None
    memory_value: str | None = None
    target_side: Literal["left", "right"] | None = None
    target_row: Literal["front", "rear"] | None = None
    place_handle: str | None = None
    choice_handle: str | None = None
    reject_candidates: StrictBool | None = None
    use_search_results: bool | None = None
    category: Literal["pharmacy", "charging"] | None = None
    nearest: bool | None = None
    destination_scope: Literal["place", "region"] | None = None
    trip_kind: Literal["one_way", "round_trip"] | None = None
    origin_query: str | None = None
    plan_id: str | None = None
    route_revision: int | None = Field(default=None, ge=1)

    requested_vehicle_model: str | None = Field(default=None, pattern=r"^VF[0-9]+$")
    requested_model_year: StrictInt | None = Field(default=None, ge=2000, le=2100)
    question_kind: Literal["charging_duration"] | None = None
    charging_mode: Literal["AC", "DC"] | None = None
    temperature_delta_celsius: StrictFloat | None = Field(default=None, ge=-30, le=30)


class ActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str
    arguments: Arguments = Field(default_factory=Arguments)
    excluded_zones: list[Literal["driver", "front_passenger", "rear_left", "rear_right"]] | None = None
    exclusion_mode: Literal["add", "replace"] = "add"
    referenced_target: bool = False

    @model_validator(mode="after")
    def capability_arguments(self):
        from src.vivi.agents.tools import TOOL_REGISTRY

        spec = TOOL_REGISTRY.get(self.capability)
        if spec is not None:
            allowed = set(spec.parameters.model_fields)
            if self.capability == "climate.set_temperature":
                allowed.add("temperature_delta_celsius")
            if self.capability in {"door.set_open", "door.set_lock", "window.set_position", "seat.set_heat_level"}:
                allowed.update({"target_side", "target_row"})
            supplied = set(self.arguments.model_dump(exclude_none=True))
            if supplied - allowed:
                raise ValueError(f"Unsupported arguments for {self.capability}: {sorted(supplied - allowed)}")
        return self


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_index: int = Field(ge=0, le=4)
    action: ActionDraft


class SemanticTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    act: Literal[
        "request",
        "modify",
        "confirm",
        "deny",
        "cancel",
        "question",
        "reply",
        "resume",
        "suspend",
        "clarify",
        "unsupported",
        "offer",
    ]
    task_ref: Literal["active", "suspended:0", "suspended:1", "suspended:2"] | None = None
    step_index: int | None = Field(default=None, ge=0, le=4)
    actions: list[ActionDraft] = Field(default_factory=list, max_length=5)
    step_updates: list[StepUpdate] = Field(default_factory=list, max_length=5)
    append_actions: list[ActionDraft] = Field(default_factory=list, max_length=5)
    clarification_drafts: list[ActionDraft] = Field(default_factory=list, max_length=5)
    text: str = ""
    negated: bool = False
    conditional: bool = False
    scheduled: bool = False
    conflicting: bool = False
    unresolved: list[str] = Field(default_factory=list, max_length=5)

    def drafts(self):
        return [*self.actions, *(patch.action for patch in self.step_updates), *self.append_actions]

    @model_validator(mode="after")
    def coherent_act(self):
        if (self.step_updates or self.append_actions) and self.act != "modify":
            raise ValueError("Step patches and appended steps require modify")
        if self.step_updates and self.actions:
            raise ValueError("Use explicit step_updates or legacy ordered actions, not both")
        if len(self.actions) + len(self.step_updates) + len(self.append_actions) > 5:
            raise ValueError("At most five changes per turn")
        if len({patch.step_index for patch in self.step_updates}) != len(self.step_updates):
            raise ValueError("Duplicate step update")
        if (
            self.act in {"confirm", "deny", "cancel", "reply", "resume", "suspend", "clarify", "unsupported"}
            and self.actions
        ):
            raise ValueError(
                "Only request/question/modify/offer may carry action drafts; known actions with missing slots use request, not clarify"
            )
        return self


class TurnEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    turn_id: str
    text: str = ""
    provider: str = "rules"
    kind: Literal["utterance", "confirmation", "action", "navigation"] = "utterance"
    confirmation_id: str | None = None
    confirmation_decision: Literal["approve", "deny"] | None = None
    action: ActionDraft | None = None
    navigation_event: dict | None = None
    position: dict | None = None
    vehicle_model: str = "VF8"
    model_year: int = 2026
    locale: str = "vi_vn"


StepStatus = Literal[
    "pending",
    "waiting_slot",
    "waiting_confirmation",
    "approved",
    "running",
    "completed",
    "blocked",
    "unknown",
    "cancelled",
    "offer",
]


class TaskStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    draft: ActionDraft
    status: StepStatus = "pending"
    confirmation: dict | None = None
    result: dict | None = None
    command_id: str | None = None
    resolved_arguments: dict | None = None
    missing_slots: list[str] = Field(default_factory=list)


class DialogueTask(BaseModel):
    contract_version: int = 0
    id: str = Field(default_factory=lambda: str(uuid4()))
    revision: int = 1
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=3))
    steps: list[TaskStep]
    source_text: str = ""

    def current_index(self):
        return next((i for i, step in enumerate(self.steps) if step.status not in {"completed", "cancelled"}), None)


class ClarificationState(BaseModel):
    source_text: str
    question: str
    drafts: list[ActionDraft] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=3))


class NavigationState(BaseModel):
    request_id: str = ""
    revision: int = 0
    event_sequence: int = 0
    status: Literal["idle", "selecting", "preview", "running", "charging", "paused", "completed"] = "idle"
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
    applied_events: list[str] = Field(default_factory=list)


class TripPlanState(NavigationState):
    """A calculation independent of the route currently being driven."""

    id: str = Field(default_factory=lambda: "trip:" + str(uuid4()))
    purpose: Literal["charging_itinerary"] = "charging_itinerary"
    calculation_status: Literal["draft", "ready", "incomplete", "cancelled"] = "draft"
    offer: dict | None = None
    started_revision: int | None = None
    calculation_basis: str | None = None


class SessionState(BaseModel):
    active: DialogueTask | None = None
    suspended: list[DialogueTask] = Field(default_factory=list)
    clarification: ClarificationState | None = None
    navigation: NavigationState = Field(default_factory=NavigationState)
    trip_plan: TripPlanState | None = None

    def context(self):
        return {
            "active": self.active.model_dump(mode="json") if self.active else None,
            "suspended": [task.model_dump(mode="json") for task in self.suspended],
        }


def semantic_schema():
    """Portable JSON representation; capability field compatibility is validated.

    A flat DTO avoids provider-specific limits on nested discriminated unions.
    The same server validator rejects incompatible non-null fields and requests
    one schema repair. Missing slots remain null for the reducer to collect.
    """
    from src.vivi.agents.tools import TOOL_REGISTRY

    schema = SemanticTurn.model_json_schema()
    schema["$defs"]["ActionDraft"]["properties"]["capability"]["enum"] = list(TOOL_REGISTRY)
    return schema
