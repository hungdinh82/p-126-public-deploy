"""Deterministic controls mutate accepted journeys directly, never a draft route."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from src.vivi.navigation.effects import vehicle_effect
from src.vivi.navigation.models import NavigationWorkspace, clear_navigation
from src.vivi.navigation.presentation import resume_message
from src.vivi.navigation.workflow import current_origin

CONTROL_KINDS = {"gear", "pause", "resume", "end", "clear", "history_clear"}


class NavigationTransitions:
    def __init__(self, vehicle, energy):
        self.vehicle, self.energy = vehicle, energy

    @staticmethod
    def blocked(message, code):
        return {"status": "blocked", "message": message, "error_code": code}

    async def apply(self, event, session, value):
        workspace = session.navigation_workspace
        journey = workspace.journey
        kind = value["kind"]
        if kind != "gear" and value["revision"] != workspace.revision:
            return self.blocked("Sự kiện thuộc tuyến cũ.", "stale_route"), session
        if kind == "gear":
            if value.get("gear") not in {"P", "D"}:
                return self.blocked("Thiếu trạng thái P hoặc D.", "missing_gear"), session
            if value["gear"] == "P":
                kind = "hold_charge" if journey and journey.status == "charging" else "pause"
            elif journey and journey.status == "paused":
                kind = "resume"
            elif journey and journey.status == "charging":
                return self.blocked("Xe đang sạc tới mức pin cần thiết để tiếp tục.", "charging"), session
            else:
                draft = workspace.draft
                if (
                    workspace.live_journey is None
                    and draft
                    and draft.purpose == "navigate"
                    and draft.route
                    and not draft.clarification
                    and (draft.offer or {}).get("status") == "pending"
                ):
                    # D beside an unconfirmed route must not drive off without it.
                    name = (draft.selected or {}).get("spoken_label") or (draft.selected or {}).get("name")
                    return {
                        "status": "blocked",
                        "error_code": "plan_unconfirmed",
                        "message": f"Bạn xác nhận tuyến tới {name} để bắt đầu nhé."
                        if name
                        else "Bạn xác nhận tuyến hiện tại để bắt đầu nhé.",
                        "request_action": {
                            "capability": "navigation.start",
                            "arguments": {"plan_id": draft.id, "route_revision": draft.revision},
                        },
                    }, session
                kind = "free_drive"
        if kind in {"pause", "end", "free_drive", "resume"} and not hasattr(self.vehicle, "set_driving"):
            return self.blocked("Xe chưa hỗ trợ đổi chế độ lái qua mô phỏng.", "unsupported_control"), session
        if kind == "end" and (not journey or journey.status not in {"running", "charging", "paused"}):
            return self.blocked("Không có hành trình đang hoạt động.", "not_running"), session
        if kind == "resume":
            if not journey or journey.status != "paused" or not journey.route:
                return self.blocked("Không có hành trình đang tạm dừng.", "not_paused"), session
            if journey.resume_status is None:
                return {
                    "status": "completed",
                    "message": "Bạn xác nhận lại tuyến trước khi tiếp tục nhé.",
                    "request_action": {
                        "capability": "navigation.start",
                        "arguments": {"route_revision": workspace.revision},
                    },
                }, session
            observed = await self.vehicle.get_state(event.session_id)
            if journey.resume_status == "running" and self.energy.profile:
                distance = journey.route["distance_m"]
                plan = (journey.energy or {}).get("selected_plan") or {}
                stops = plan.get("stops", [])
                if journey.stop_index < len(stops):
                    stop = stops[journey.stop_index]
                    index = stop.get("leg_index")
                    if index is None:
                        index = next(
                            (i for i, leg in enumerate(plan.get("legs", [])) if leg.get("to") == stop["station_id"]),
                            None,
                        )
                    if index is not None:
                        distance = sum(leg["distance_m"] for leg in plan["legs"][: index + 1])
                if observed.battery_percent - self.energy.soc_cost(max(0, distance - journey.progress_m)) < 15:
                    return self.blocked(
                        "Pin chưa đủ dự phòng tới điểm dừng tiếp theo. Bạn cập nhật kế hoạch sạc nhé.",
                        "insufficient_energy",
                    ), session
        try:
            if kind in {"pause", "end"}:
                await vehicle_effect(self.vehicle.set_driving, event.session_id, False)
                if journey and journey.status in {"running", "charging"}:
                    journey.resume_status, journey.status = journey.status, "paused"
                message = (
                    "Đã về P, hành trình tạm dừng. Chuyển D để tiếp tục."
                    if journey and journey.status == "paused"
                    else "Đã chuyển về P."
                )
                ui = "navigation.pause"
                if kind == "end":
                    # The car stays where the trip stopped; keep that as origin.
                    workspace.origin = await current_origin(session.navigation) or workspace.origin
                    journey.status, journey.resume_status = "ended", None
                    workspace.history = [
                        *workspace.history[-19:],
                        {
                            "id": journey.route_id,
                            "destination": journey.selected,
                            "status": "ended",
                            "distance_m": journey.progress_m,
                            "ended_at": datetime.now(UTC).isoformat(),
                        },
                    ]
                    workspace.journey = None
                    message, ui = "Đã kết thúc hành trình.", "navigation.end"
            elif kind in {"resume", "free_drive"}:
                driving = kind == "free_drive" or journey.resume_status == "running"
                version = value.get("expected_vehicle_version")
                await vehicle_effect(
                    self.vehicle.set_driving,
                    event.session_id,
                    driving,
                    **({"expected_state_version": version} if version is not None else {}),
                )
                if kind == "resume":
                    journey.status, journey.resume_status = journey.resume_status, None
                message = resume_message(journey) if kind == "resume" else "Đã chuyển D."
                ui = "navigation.resume" if kind == "resume" else "navigation.gear"
            elif kind == "clear":
                observed = await self.vehicle.get_state(event.session_id)
                if observed.driving or journey and journey.status in {"running", "charging"}:
                    return self.blocked("Chuyển P trước khi xoá hành trình.", "journey_running"), session
                origin = await current_origin(session.navigation)
                session = clear_navigation(session)
                session.navigation_workspace = NavigationWorkspace(
                    origin=origin,
                    revision=workspace.revision + 1,
                    cleared_at_revision=workspace.revision + 1,
                    history=workspace.history,
                )
                workspace = session.navigation_workspace
                message, ui = "Đã xoá tuyến và kế hoạch đã lưu trong phiên.", "navigation.clear"
            elif kind == "history_clear":
                workspace.history = []
                message, ui = "Đã xoá lịch sử hành trình trong phiên.", "navigation.history_clear"
            else:
                message, ui = "Xe đang ở P để sạc.", "navigation.gear"
        except ValueError as exc:
            return self.blocked(str(exc), "simulation_policy"), session
        workspace.event_sequence += 1
        ui_events = [{"type": ui, "id": str(uuid4()), "revision": workspace.revision}]
        if (
            value["kind"] == "gear"
            and kind == "resume"
            and ((journey.energy or {}).get("selected_plan") or {}).get("stops")
        ):
            # The gear button normally stays silent. Resuming a charging route
            # still needs its current station/remaining charge announcement.
            ui_events.append(
                {
                    "type": "navigation.next_leg",
                    "id": value["id"],
                    "revision": workspace.revision,
                    "route_id": journey.route_id,
                    "message": message,
                }
            )
        return {
            "status": "completed",
            "message": message,
            "domain_applied": True,
            "ui_events": ui_events,
            "navigation_update": session.navigation.model_dump(mode="json"),
        }, session
