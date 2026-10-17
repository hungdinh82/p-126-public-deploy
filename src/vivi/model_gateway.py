"""One provider policy, deadline and audit boundary for every model stage."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from src.vivi.turn_control import current_control


@dataclass
class ModelProvider:
    name: str
    client: object
    budget_seconds: float = 10
    unavailable_until: float = 0


@dataclass
class ModelResult:
    value: Any = None
    provider: str | None = None
    attempts: list[dict] | None = None


class ModelGateway:
    def __init__(
        self,
        providers=None,
        *,
        primary="google",
        offline=False,
        breaker_seconds=30,
        fallback_chains=None,
        provider_allowed=None,
        result_recorder=None,
        provider_details=None,
    ):
        self.providers = {
            name: value
            if isinstance(value, ModelProvider)
            else ModelProvider(name, value.client, getattr(value, "budget_seconds", 10))
            for name, value in (providers or {}).items()
        }
        self.primary, self.offline = primary, offline
        self.breaker_seconds = breaker_seconds
        self.fallback_chains = fallback_chains or {}
        self.provider_allowed = provider_allowed
        self.result_recorder = result_recorder
        self.provider_details = provider_details or {}

    def _attempt_details(self, name, provider):
        configured = self.provider_details.get(name, {})
        client = getattr(provider, "client", None)
        return {
            "requested_model": configured.get("model") or getattr(client, "model", None),
            "served_model": None,
            "location": configured.get("location")
            or getattr(client, "execution_location", None)
            or ("cloud" if name in {"google", "openai", "openrouter"} else "laptop"),
        }

    def chain(self, requested=None):
        if self.offline:
            return ["local"]
        requested = requested or self.primary
        return list(
            dict.fromkeys(
                self.fallback_chains.get(requested, [requested] if requested == "local" else [requested, "local"])
            )
        )

    async def run(
        self,
        stage: str,
        operation: Callable[[ModelProvider, list[dict]], Awaitable],
        *,
        requested=None,
        budget_seconds=20,
        on_attempt=None,
    ):
        control = current_control.get()
        deadline = time.monotonic() + budget_seconds
        if control:
            deadline = min(deadline, control.deadline)
        attempts = []
        requested = requested or self.primary
        chain = self.chain(requested)

        def record_attempt(attempt):
            if on_attempt is not None:
                outcome = attempt["outcome"]
                status = "calling" if outcome == "started" else outcome
                on_attempt({**attempt, "status": status, "outcome": status})

        for index, name in enumerate(chain):
            provider = self.providers.get(name)
            details = {"attempt": index + 1, **self._attempt_details(name, provider)}
            remaining = deadline - time.monotonic()
            if control and control.cancelled.is_set():
                raise asyncio.CancelledError()
            if remaining <= 0:
                attempts.append(
                    {
                        **details,
                        "stage": stage,
                        "requested_provider": requested,
                        "provider": name,
                        "outcome": "budget_exhausted",
                        "latency_ms": 0,
                    }
                )
                record_attempt(attempts[-1])
                break
            if provider is None:
                attempts.append(
                    {
                        **details,
                        "stage": stage,
                        "requested_provider": requested,
                        "provider": name,
                        "outcome": "not_configured",
                        "latency_ms": 0,
                    }
                )
                record_attempt(attempts[-1])
                continue
            # Connectivity can restrict eligibility, but cannot reorder the
            # configured cloud -> local policy or create a second routing layer.
            if self.provider_allowed is not None and not self.provider_allowed(name):
                attempts.append(
                    {
                        **details,
                        "stage": stage,
                        "requested_provider": requested,
                        "provider": name,
                        "outcome": "unavailable",
                        "reason": "connectivity_or_profile",
                        "calls": 0,
                        "latency_ms": 0,
                    }
                )
                if control:
                    control.model_attempts.append(attempts[-1].copy())
                record_attempt(attempts[-1])
                continue
            if time.monotonic() < provider.unavailable_until:
                attempts.append(
                    {
                        **details,
                        "stage": stage,
                        "requested_provider": requested,
                        "provider": name,
                        "outcome": "circuit_open",
                        "latency_ms": 0,
                    }
                )
                record_attempt(attempts[-1])
                continue
            started = time.monotonic()
            available = sum(n in self.providers for n in chain[index:])
            attempt = {
                **details,
                "stage": stage,
                "requested_provider": requested,
                "provider": name,
                "outcome": "started",
                "calls": 0,
                "latency_ms": 0,
                "budget_seconds": min(10, provider.budget_seconds, remaining / max(1, available)),
            }
            attempts.append(attempt)
            record_attempt(attempt)
            try:
                limit = asyncio.timeout(attempt["budget_seconds"])
                async with limit:
                    value = await operation(provider, attempts)
            except Exception as exc:
                attempt.update(
                    outcome="fallback",
                    reason=type(exc).__name__,
                    budget_limited=limit.expired(),
                    http_status=getattr(getattr(exc, "response", None), "status_code", None),
                )
                if isinstance(exc, ValidationError):
                    attempt["schema_errors"] = [
                        {"field": list(e["loc"]), "code": e["type"], "message": e["msg"][:300]}
                        for e in exc.errors(include_input=False, include_context=False, include_url=False)
                    ]
                # Bad content does not mean that the transport is unhealthy.
                if not isinstance(exc, ValueError) and not limit.expired():
                    cooldown = self.breaker_seconds
                    if name == "local" and attempt["http_status"] in {429, 503}:
                        cooldown = min(1, cooldown)
                    provider.unavailable_until = time.monotonic() + cooldown
            else:
                if attempt["outcome"] == "started":
                    attempt["outcome"] = "served"
                served_model = getattr(provider.client, "served_model", None)
                attempt["served_model"] = served_model if isinstance(served_model, str) and served_model else None
                return ModelResult(value, name, attempts)
            finally:
                attempt["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                record_attempt(attempt)
                if control:
                    control.model_attempts = [*control.model_attempts, attempt.copy()]
                if self.result_recorder is not None:
                    self.result_recorder(name, attempt["outcome"], attempt["latency_ms"], attempt.get("reason"))
        return ModelResult(attempts=attempts)


def record_model_call(attempts, *, repair=False):
    """Adapters call this immediately before each actual model request."""
    attempts[-1]["calls"] += 1
    if repair:
        attempts[-1]["repairs"] = attempts[-1].get("repairs", 0) + 1
