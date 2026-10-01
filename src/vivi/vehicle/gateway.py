from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from src.vivi.agents.contracts import ActionProposal
from src.vivi.domain.confirmations import (
    ConfirmationResolution,
    ConfirmationStore,
    action_fingerprint,
)
from src.vivi.domain.models import ActionProposal as DomainActionProposal
from src.vivi.domain.models import VehicleState
from src.vivi.domain.safety import validate
from src.vivi.vehicle.memory import VehicleSimulator


@dataclass
class GatewayDecision:
    allowed: bool
    status: str
    message: str
    risk_class: str
    confirmation: dict[str, Any] | None = None


@dataclass
class GatewayExecution:
    executed: bool
    verified: bool
    message: str
    vehicle_state: dict[str, Any]
    error: str | None = None


class VehicleActionGateway:
    """Reuse the ViVi policy/simulator without coupling them to intent classification."""

    def __init__(
        self,
        vehicle: VehicleSimulator | None = None,
        confirmations: ConfirmationStore | None = None,
    ) -> None:
        self.vehicle = vehicle or VehicleSimulator()
        self.confirmations = confirmations or ConfirmationStore()
        self._confirmation_cache: dict[
            tuple[str, str], tuple[str, str, ConfirmationResolution]
        ] = {}
        self._safety_cache: dict[tuple[str, str], tuple[str, GatewayDecision]] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _domain_action(proposal: ActionProposal) -> DomainActionProposal:
        return DomainActionProposal(
            intent=proposal.intent,
            arguments=proposal.arguments,
            confidence=proposal.confidence,
        )

    def check(
        self,
        session_id: str,
        proposal: ActionProposal,
        supplied_vehicle_state: dict[str, Any] | None = None,
        *,
        confirmed: bool = False,
        turn_id: str | None = None,
    ) -> GatewayDecision:
        action = self._domain_action(proposal)
        fingerprint = action_fingerprint(action)
        cache_key = (session_id, turn_id) if turn_id and not confirmed else None
        if cache_key is not None:
            with self._cache_lock:
                cached = self._safety_cache.get(cache_key)
            if cached is not None:
                cached_fingerprint, cached_decision = cached
                if cached_fingerprint != fingerprint:
                    return GatewayDecision(
                        allowed=False,
                        status="blocked",
                        message="turn_id đã được dùng cho một thao tác khác.",
                        risk_class="R3",
                    )
                return cached_decision
        # Browser/model-provided state is context only. The adapter remains the
        # source of truth at the policy boundary.
        del supplied_vehicle_state
        vehicle_state = self.vehicle.state_for(session_id)
        result = validate(action, vehicle_state, confirmed=confirmed)
        confirmation = None
        if result.requires_confirmation:
            preview = self.confirmations.create(
                session_id,
                action,
                result.preview or result.message,
                vehicle_state,
            )
            confirmation = preview.model_dump(mode="json")
        decision = GatewayDecision(
            allowed=result.allowed,
            status=result.status,
            message=result.message,
            risk_class=result.risk_class,
            confirmation=confirmation,
        )
        if cache_key is not None:
            with self._cache_lock:
                self._safety_cache[cache_key] = (fingerprint, decision)
        return decision

    def resolve_confirmation(
        self,
        session_id: str,
        turn_id: str,
        confirmation_id: str,
        decision: str,
    ) -> tuple[ActionProposal | None, GatewayDecision]:
        cache_key = (session_id, turn_id)
        with self._cache_lock:
            cached = self._confirmation_cache.get(cache_key)
            if cached is None:
                resolution = self.confirmations.resolve(
                    confirmation_id,
                    session_id,
                    decision,
                    self.vehicle.state_for(session_id),
                )
                self._confirmation_cache[cache_key] = (confirmation_id, decision, resolution)
            else:
                original_id, original_decision, resolution = cached
                if (original_id, original_decision) != (confirmation_id, decision):
                    return None, GatewayDecision(
                        allowed=False,
                        status="blocked",
                        message="turn_id đã được dùng cho một quyết định xác nhận khác.",
                        risk_class="R3",
                    )
        if resolution.action is None:
            return None, GatewayDecision(
                allowed=False,
                status=resolution.status,
                message=resolution.message,
                risk_class="R2",
            )
        proposal = ActionProposal(
            intent=resolution.action.intent,
            arguments=resolution.action.arguments,
            confidence=resolution.action.confidence or 1.0,
        )
        checked = self.check(session_id, proposal, confirmed=True)
        return proposal, checked

    def execute(
        self,
        session_id: str,
        turn_id: str,
        proposal: ActionProposal,
    ) -> GatewayExecution:
        action = self._domain_action(proposal)
        try:
            vehicle_state, message = self.vehicle.execute_sync(session_id, turn_id, action)
            verified = self._verify(proposal, vehicle_state)
            return GatewayExecution(
                executed=True,
                verified=verified,
                message=message if verified else "Xe chưa phản ánh đúng trạng thái được yêu cầu.",
                vehicle_state=vehicle_state.model_dump(mode="json"),
                error=None if verified else "vehicle state verification failed",
            )
        except Exception as exc:
            current = self.vehicle.state_for(session_id)
            return GatewayExecution(
                executed=False,
                verified=False,
                message="Chưa thể xác minh thao tác trên xe mô phỏng.",
                vehicle_state=current.model_dump(mode="json"),
                error=str(exc),
            )

    @staticmethod
    def _verify(proposal: ActionProposal, state: VehicleState) -> bool:
        arguments = proposal.arguments
        if proposal.intent == "climate.set_temperature":
            return state.temperature_celsius == float(arguments["value_celsius"])
        if proposal.intent == "window.set_position":
            return state.window_driver_percent == int(arguments["position_percent"])
        if proposal.intent == "door.set_open":
            return state.door_driver_open is arguments["open"]
        if proposal.intent == "door.set_lock":
            return state.door_driver_locked is arguments["locked"]
        if proposal.intent == "seat.set_heat_level":
            return state.seat_driver_heat_level == arguments["level"]
        if proposal.intent == "media.play":
            return state.media_playing is True
        if proposal.intent == "media.pause":
            return state.media_playing is False
        return proposal.intent == "vehicle.get_status"
