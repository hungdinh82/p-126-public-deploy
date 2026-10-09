from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
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
from src.vivi.turn_control import current_control
from src.vivi.vehicle.memory import VehicleSimulator
from src.vivi.vehicle.zones import BODY_PANELS, CABIN_ZONES, selected_zones


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
        ledger=None,
    ) -> None:
        self.vehicle = vehicle or VehicleSimulator()
        self.confirmations = confirmations or ConfirmationStore()
        self.ledger = ledger
        self._execute_lock = threading.Lock()
        self._confirmation_cache: dict[
            tuple[str, str], tuple[str, str, ConfirmationResolution, VehicleState]
        ] = {}
        self._safety_cache: dict[tuple[str, str], tuple[str, GatewayDecision]] = {}
        self._cache_lock = threading.Lock()
        self._confirmation_checks: dict[tuple[str, str], GatewayDecision] = {}
        self._executions: dict[tuple[str, str], tuple[str, GatewayExecution]] = {}
        self._authorizations: dict[tuple[str, str], tuple[str, str, int]] = {}

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
        approved_snapshot: VehicleState | None = None,
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
        if approved_snapshot and (vehicle_state.vehicle_id, vehicle_state.state_version) != (approved_snapshot.vehicle_id, approved_snapshot.state_version):
            return GatewayDecision(False, "denied", "Trạng thái xe đã thay đổi. Bạn xác nhận lại thao tác nhé.", "R2")
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
        if turn_id and decision.allowed:
            with self._cache_lock:
                self._authorizations[(session_id, turn_id)] = (fingerprint, vehicle_state.vehicle_id, vehicle_state.state_version)
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
                approved_snapshot = self.vehicle.state_for(session_id)
                resolution = self.confirmations.resolve(
                    confirmation_id,
                    session_id,
                    decision,
                    approved_snapshot,
                )
                self._confirmation_cache[cache_key] = (confirmation_id, decision, resolution, approved_snapshot)
            else:
                original_id, original_decision, resolution, approved_snapshot = cached
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
        with self._cache_lock:
            checked = self._confirmation_checks.get(cache_key)
        if checked is None:
            checked = self.check(session_id, proposal, confirmed=True, turn_id=turn_id, approved_snapshot=approved_snapshot)
            with self._cache_lock:
                self._confirmation_checks[cache_key] = checked
        return proposal, checked

    def execute(
        self,
        session_id: str,
        turn_id: str,
        proposal: ActionProposal,
    ) -> GatewayExecution:
        with self._execute_lock:
            return self._execute(session_id, turn_id, proposal)

    def _execute(self, session_id, turn_id, proposal):
        action = self._domain_action(proposal)
        try:
            key = (session_id, turn_id)
            cached_execution = self._executions.get(key)
            if cached_execution:
                if cached_execution[0] != action_fingerprint(action):
                    raise ValueError("Command ID already belongs to another action")
                return cached_execution[1]
            current = self.vehicle.state_for(session_id)
            if self.ledger:
                completed = self.ledger.command_result(session_id, turn_id)
                if completed:
                    if completed["fingerprint"] != self.ledger.action_key(action):
                        raise ValueError("Command ID already belongs to another action")
                    return GatewayExecution(**completed["result"])
            with self._cache_lock:
                authorization = self._authorizations.get((session_id, turn_id))
            if (self.ledger or authorization) and authorization != (action_fingerprint(action), current.vehicle_id, current.state_version):
                return GatewayExecution(False, False, "Trạng thái xe đã thay đổi. Bạn xác nhận lại thao tác nhé.",
                    current.model_dump(mode="json"), "missing or stale dispatch authorization")
            policy = validate(action, current, confirmed=True)
            if not policy.allowed:
                return GatewayExecution(False, False, policy.message, current.model_dump(mode="json"), "policy changed before dispatch")
            control = current_control.get()
            if control:
                control.allow_dispatch()
            if self.ledger:
                cached = self.ledger.reserve_command(session_id, turn_id, {"intent": action.intent, "arguments": action.arguments})
                if cached:
                    return GatewayExecution(**cached)
            options = {"expected_state_version": current.state_version} if self.ledger else {}
            vehicle_state, message = self.vehicle.execute_sync(session_id, turn_id, action, **options)
            verified = self._verify(proposal, vehicle_state)
            result = GatewayExecution(
                executed=True,
                verified=verified,
                message=message if verified else "Xe chưa phản ánh đúng trạng thái được yêu cầu.",
                vehicle_state=vehicle_state.model_dump(mode="json"),
                error=None if verified else "vehicle state verification failed",
            )
            if self.ledger:
                self.ledger.finish_command(session_id, turn_id, asdict(result))
            self._executions[key] = (action_fingerprint(action), result)
            return result
        except Exception as exc:
            try:
                current = self.vehicle.state_for(session_id)
                snapshot = current.model_dump(mode="json")
            except Exception:
                snapshot = {}
            return GatewayExecution(
                executed=False,
                verified=False,
                message="Chưa thể xác minh thao tác trên xe mô phỏng.",
                vehicle_state=snapshot,
                error=str(exc),
            )

    @staticmethod
    def _verify(proposal: ActionProposal, state: VehicleState) -> bool:
        arguments = proposal.arguments
        if proposal.intent == "climate.set_temperature":
            return state.temperature_celsius == float(arguments["value_celsius"])
        if proposal.intent == "window.set_position":
            return all(
                getattr(state.window_positions, zone) == int(arguments["position_percent"])
                for zone in selected_zones(arguments)
            )
        if proposal.intent == "door.set_open":
            return all(getattr(state.door_states, zone).open is arguments["open"] for zone in selected_zones(arguments))
        if proposal.intent == "door.set_lock":
            return all(
                getattr(state.door_states, zone).locked is arguments["locked"] for zone in selected_zones(arguments)
            )
        if proposal.intent in BODY_PANELS:
            return getattr(state, BODY_PANELS[proposal.intent][0]) is arguments["open"]
        if proposal.intent == "body.set_open":
            opening = arguments["open"]
            return state.hood_open is opening and state.trunk_open is opening and all(
                getattr(state.door_states, zone).open is opening for zone in CABIN_ZONES
            )
        if proposal.intent == "seat.set_heat_level":
            return all(
                getattr(state.seat_heat_levels, zone) == arguments["level"] for zone in selected_zones(arguments)
            )
        if proposal.intent == "media.play":
            return state.media_playing is True
        if proposal.intent == "media.pause":
            return state.media_playing is False
        return proposal.intent == "vehicle.get_status"
