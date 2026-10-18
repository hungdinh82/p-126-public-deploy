"""Authored inputs and independent oracles; never derive expectations from output."""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.vivi.agents.tools import TOOL_REGISTRY

OPERATORS = {"contains", "not_contains", "length", "min", "exists", "one_of", "same_members", "same_calls", "decisions", "ordered_decisions"}


def validate_expectations(expect):
    for path, rule in expect.items():
        if not path or not isinstance(path, str):
            raise ValueError("Assertion paths must be non-empty strings")
        if isinstance(rule, dict) and (not rule or not set(rule) <= OPERATORS):
            raise ValueError(f"Unknown/empty assertion operators: {path}: {rule}")


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    fixture_attempts: list[list[Call]] = Field(default_factory=list)
    fixture_error: bool = False
    expect: dict[str, Any] = Field(min_length=1)

    @model_validator(mode="after")
    def assertions(self):
        validate_expectations(self.expect)
        return self


class AgentCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    feature: str = Field(min_length=1)
    input_style: str = "direct"
    tool_names: list[str] = Field(default_factory=list)
    covers: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    initial_vehicle: dict[str, Any] = Field(default_factory=dict)
    dependencies: Literal["unavailable", "synthetic"] = "unavailable"
    turns: list[Turn] = Field(min_length=1)

    @model_validator(mode="after")
    def capabilities(self):
        if set(self.covers) - TOOL_REGISTRY.keys():
            raise ValueError(f"Unknown capabilities in {self.id}: {self.covers}")
        return self


class LanguageCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    feature: str = Field(min_length=1)
    input_style: str = "direct"
    tool_names: list[str] = Field(default_factory=list)
    covers: list[str] = Field(min_length=1)
    text: str = Field(min_length=1)
    session: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list)
    observation: dict[str, Any] | None = None
    local_only: bool = False
    local_tool_expect: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(min_length=1)

    @model_validator(mode="after")
    def assertions(self):
        validate_expectations(self.expect)
        if set(self.covers) - TOOL_REGISTRY.keys():
            raise ValueError(f"Unknown capabilities in {self.id}")
        return self


def load(path: Path, layer: Literal["tools", "language"]):
    model = AgentCase if layer == "tools" else LanguageCase
    result, seen = [], set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = model.model_validate(json.loads(line))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
        if case.id in seen:
            raise ValueError(f"Duplicate case ID: {case.id}")
        seen.add(case.id)
        result.append(case)
    if not result:
        raise ValueError(f"Empty dataset: {path}")
    return result
