"""Stable capability result envelope; legacy fields remain available to adapters."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: Literal["completed", "waiting_slot", "waiting_confirmation", "blocked", "unknown"]
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    effect_receipt: dict[str, Any] | None = None
    error_code: str | None = None
    failure_kind: Literal["validation", "policy", "dependency", "unverified"] | None = None
    resource: dict[str, Any] | None = None

    @classmethod
    def normalize(cls, value, *, capability=None, command_id=None):
        payload = dict(value)
        payload.setdefault(
            "data",
            {
                key: payload[key]
                for key in ("citations", "grounding_status", "vehicle_state", "ui_events")
                if key in payload
            },
        )
        payload.setdefault("effect_receipt", payload.get("execution") or payload.get("receipt"))
        if payload.get("error_code") is None and payload["status"] in {"blocked", "unknown"}:
            payload["error_code"] = "capability_blocked" if payload["status"] == "blocked" else "effect_unverified"
        if payload.get("failure_kind") is None:
            if payload["status"] == "unknown":
                payload["failure_kind"] = "unverified"
            elif payload["status"] == "blocked":
                code = payload.get("error_code", "")
                payload["failure_kind"] = (
                    "dependency"
                    if any(word in code for word in ("unavailable", "timeout", "not_configured"))
                    else "validation"
                    if code.startswith("stale_")
                    else "policy"
                )
        if capability:
            payload.setdefault("resource", {"capability": capability, "command_id": command_id})
        return cls.model_validate(payload).model_dump(mode="json")
