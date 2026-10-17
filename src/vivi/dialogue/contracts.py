"""Semantic proposals and checkpoint-owned dialogue state.

Providers describe requests; only the reducer owns task identities and revisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, model_validator

from src.vivi.navigation.contracts import DestinationSpec
from src.vivi.navigation.models import (
    ActiveJourney as ActiveJourney,
)
from src.vivi.navigation.models import (
    NavigationState as NavigationState,
)
from src.vivi.navigation.models import (
    NavigationWorkspace as NavigationWorkspace,
)
from src.vivi.navigation.models import (
    PlanDraft as PlanDraft,
)
from src.vivi.navigation.models import (
    RouteSnapshot as RouteSnapshot,
)
from src.vivi.navigation.models import (
    TripPlanState as TripPlanState,
)


class Arguments(BaseModel):
    """Normalized domain slots; registry schemas validate execution.

    The local operations model receives small per-tool schemas, not this union.
    Null defaults in a serialized SemanticTurn can be host-added values.
    """

    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    destination: DestinationSpec | None = None
    mode: Literal["chat", "general", "creative"] | None = None
    location: str | None = Field(default=None, max_length=200)
    forecast_day: Literal["today", "tomorrow", "day_after_tomorrow"] | None = None
    gear: Literal["P", "D"] | None = None
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
    purpose: Literal["navigate", "itinerary"] | None = None
    origin_query: str | None = None
    plan_id: str | None = None
    route_revision: int | None = Field(default=None, ge=1)

    requested_vehicle_model: str | None = Field(default=None, pattern=r"^VF[0-9]+$")
    requested_model_year: StrictInt | None = Field(default=None, ge=2000, le=2100)
    question_kind: Literal["charging_duration"] | None = None
    charging_mode: Literal["AC", "DC"] | None = None
    temperature_delta_celsius: StrictFloat | None = Field(default=None, ge=-30, le=30)
    # Direction of an amountless comfort request ("tôi lạnh"); the host asks the
    # target rather than guessing a step, and ignores it once value_celsius is set.
    temperature_direction: Literal["increase", "decrease"] | None = None


class ActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str
    arguments: Arguments = Field(default_factory=Arguments)
    excluded_zones: list[Literal["driver", "front_passenger", "rear_left", "rear_right"]] | None = None
    exclusion_mode: Literal["add", "replace"] = "add"
    referenced_target: bool = False

    @model_validator(mode="after")
    def capability_arguments(self):
        from src.vivi.agents.tools import TOOL_REGISTRY, semantic_argument_names

        spec = TOOL_REGISTRY.get(self.capability)
        if spec is not None:
            if (
                self.arguments.destination
                and self.arguments.destination.selection != "reference"
                and not self.arguments.query
            ):
                self.arguments.query = self.arguments.destination.query() or None
            allowed = semantic_argument_names(self.capability)
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
        "answer",
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
    question_answer: QuestionReply | None = None
    consent_question_id: str | None = None
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
        if (self.act == "answer") != (self.question_answer is not None):
            raise ValueError("answer requires question_answer; other acts cannot carry one")
        if (self.step_updates or self.append_actions) and self.act != "modify":
            raise ValueError("Step patches and appended steps require modify")
        if self.step_updates and self.actions:
            raise ValueError("Use explicit step_updates or legacy ordered actions, not both")
        if len(self.actions) + len(self.step_updates) + len(self.append_actions) > 5:
            raise ValueError("At most five changes per turn")
        if len({patch.step_index for patch in self.step_updates}) != len(self.step_updates):
            raise ValueError("Duplicate step update")
        if (
            self.act in {"answer", "confirm", "deny", "cancel", "reply", "resume", "suspend", "clarify", "unsupported"}
            and self.actions
        ):
            raise ValueError(
                "Only request/question/modify/offer may carry action drafts; known actions with missing slots use request, not clarify"
            )
        return self


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    role: str = "choice"
    effect: ActionDraft


class QuestionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Literal["collect_slot", "select_entity", "confirm_proposal", "approve_execution"]
    version: Literal[1] = 1
    owner: str
    target_id: str
    revision: int
    kind: str
    text: str
    slot: str | None = None
    slot_effect: ActionDraft | None = None
    destination: DestinationSpec | None = None
    options: list[QuestionOption] = Field(default_factory=list)
    yes_effect: ActionDraft | None = None
    no_effect: ActionDraft | None = None
    yes_act: Literal["confirm", "deny"] | None = None
    no_act: Literal["confirm", "deny"] | None = None


class PendingQuestion(QuestionProposal):
    id: str
    task_id: str | None = None
    step_id: str | None = None
    expires_at: datetime
    binding: str
    source_binding: str = ""


class QuestionReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1, max_length=100)
    option_id: str | None = Field(default=None, min_length=1, max_length=100)
    value: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def option_or_value(self):
        if (self.option_id is None) == (self.value is None):
            raise ValueError("Provide one option_id or slot value")
        return self


class TurnEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    turn_id: str
    text: str = ""
    provider: str = "google"
    kind: Literal["utterance", "action", "navigation"] = "utterance"
    action: ActionDraft | None = None
    question_reply: QuestionReply | None = None
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


class SessionState(BaseModel):
    domain_commands: list[dict] = Field(default_factory=list, exclude=True)
    dialogue_question: PendingQuestion | None = None
    control_revision: int = 0
    snapshot_sequence: int = 0
    control_receipts: list[dict] = Field(default_factory=list)
    active: DialogueTask | None = None
    suspended: list[DialogueTask] = Field(default_factory=list)
    clarification: ClarificationState | None = None
    navigation_workspace: NavigationWorkspace = Field(default_factory=NavigationWorkspace)
    navigation_focus: Literal["navigation", "trip"] = "navigation"
    conversation_context: dict = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_navigation(cls, value):
        if isinstance(value, dict) and "trip_plan" in value:
            value = dict(value)
            trip = value.pop("trip_plan")
            if trip:
                workspace = NavigationWorkspace.model_validate(value.get("navigation_workspace") or {})
                workspace.draft = PlanDraft.model_validate(
                    {**(trip.model_dump(mode="json") if hasattr(trip, "model_dump") else trip), "purpose": "itinerary"}
                )
                value["navigation_workspace"] = workspace
        if isinstance(value, dict) and "navigation" in value:
            value = dict(value)
            legacy = value.pop("navigation")
            if "navigation_workspace" not in value:
                value["navigation_workspace"] = NavigationWorkspace.from_snapshot(
                    NavigationState.model_validate(legacy)
                )
        return value

    @property
    def navigation(self):
        return self.navigation_workspace.snapshot()

    @navigation.setter
    def navigation(self, value):
        snapshot = NavigationState.model_validate(value.model_dump(mode="json"))
        self.navigation_workspace = NavigationWorkspace.from_snapshot(snapshot, self.navigation_workspace)

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

    def pending_question(self):
        from src.vivi.dialogue.questions import current_question, legacy_view

        question = current_question(self)
        return legacy_view(question) if question else None

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
    schema["properties"].pop("consent_question_id", None)
    schema["$defs"]["ActionDraft"]["properties"]["capability"]["enum"] = [
        name for name in TOOL_REGISTRY if not name.startswith("trip.")
    ]
    return schema


SemanticTurn.model_rebuild()
