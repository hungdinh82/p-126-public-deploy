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
    referenced_target: bool = False

    @model_validator(mode="after")
    def capability_arguments(self):
        from src.vivi.agents.tools import TOOL_REGISTRY
        spec = TOOL_REGISTRY.get(self.capability)
        if spec is not None:
            allowed = set(spec.parameters.model_fields)
            if self.capability == "climate.set_temperature":
                allowed.add("temperature_delta_celsius")
            supplied = set(self.arguments.model_dump(exclude_none=True))
            if supplied - allowed:
                raise ValueError(f"Unsupported arguments for {self.capability}: {sorted(supplied - allowed)}")
        return self


class SemanticTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    act: Literal["request", "modify", "confirm", "deny", "cancel", "question", "reply", "resume", "suspend", "clarify", "unsupported", "offer"]
    task_ref: Literal["active", "suspended:0", "suspended:1", "suspended:2"] | None = None
    step_index: int | None = Field(default=None, ge=0, le=4)
    actions: list[ActionDraft] = Field(default_factory=list, max_length=5)
    text: str = ""
    negated: bool = False
    conditional: bool = False
    scheduled: bool = False
    conflicting: bool = False
    unresolved: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def coherent_act(self):
        if self.act in {"confirm", "deny", "cancel", "reply", "resume", "suspend", "clarify", "unsupported"} and self.actions:
            raise ValueError("Only request/question/modify/offer may carry action drafts; known actions with missing slots use request, not clarify")
        return self


class TurnEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    turn_id: str
    text: str = ""
    provider: str = "rules"
    kind: Literal["utterance", "confirmation", "action"] = "utterance"
    confirmation_id: str | None = None
    confirmation_decision: Literal["approve", "deny"] | None = None
    action: ActionDraft | None = None
    vehicle_model: str = "VF8"
    model_year: int = 2026
    locale: str = "vi_vn"


StepStatus = Literal["pending", "waiting_slot", "waiting_confirmation", "approved", "running", "completed", "blocked", "unknown", "cancelled", "offer"]


class TaskStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    draft: ActionDraft
    status: StepStatus = "pending"
    confirmation: dict | None = None
    result: dict | None = None
    command_id: str | None = None
    resolved_arguments: dict | None = None


class DialogueTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    revision: int = 1
    expires_at: datetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=3))
    steps: list[TaskStep]

    def current_index(self):
        return next((i for i, step in enumerate(self.steps) if step.status not in {"completed", "cancelled"}), None)


class SessionState(BaseModel):
    active: DialogueTask | None = None
    suspended: list[DialogueTask] = Field(default_factory=list)

    def context(self):
        return {"active": self.active.model_dump(mode="json") if self.active else None,
                "suspended": [task.model_dump(mode="json") for task in self.suspended]}


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
