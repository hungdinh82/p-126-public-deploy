"""The sole writer of checkpoint-owned task state; no I/O or language matching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.vivi.capabilities.registry import (
    compatible_change,
    merge_arguments,
    merge_exclusions,
    transferable_arguments,
)
from src.vivi.dialogue.contracts import (
    ActionDraft,
    ClarificationState,
    DialogueTask,
    SemanticTurn,
    SessionState,
    TaskStep,
)


def expire(session: SessionState):
    state = session.model_copy(deep=True)
    state.suspended = [task for task in state.suspended if task.expires_at > datetime.now(UTC)]
    if state.active and state.active.expires_at <= datetime.now(UTC):
        state.active = None
    if state.clarification and state.clarification.expires_at <= datetime.now(UTC):
        state.clarification = None
    return state


def reject_modification(state, task, message):
    """An ambiguous correction changes no slots and revokes prior consent."""
    grants = [s for s in task.steps if s.status in {"waiting_confirmation", "approved"}]
    if grants:
        task.revision += 1
        for step in grants:
            step.status, step.confirmation, step.command_id, step.resolved_arguments = "pending", None, None, None
    return state, message


def reduce_turn(
    session: SessionState, value: SemanticTurn, *, revoke_consent_on_clarify=True
) -> tuple[SessionState, str | None]:
    state = expire(session)
    # A yes/no answers the current typed place question before execution consent.
    index = state.active.current_index() if state.active else None
    current = state.active.steps[index] if index is not None else None
    if (
        value.act in {"confirm", "deny"}
        and current
        and current.status == "waiting_slot"
        and current.draft.capability.startswith(("navigation.", "trip."))
    ):
        prefix = current.draft.capability.split(".")[0]
        owner = state.trip_plan if prefix == "trip" else state.navigation
        args = {"plan_id": owner.id, "route_revision": owner.revision} if prefix == "trip" else {}
        question = owner.clarification or {}
        action = None
        if question.get("phase") == "change_destination":
            kind = "specific" if value.act == "confirm" else "representative"
            choice = next((c for c in question.get("choices", []) if c["kind"] == kind), None)
            if choice:
                action = ActionDraft(
                    capability=f"{prefix}.refine", arguments={**args, "choice_handle": choice["handle"]}
                )
        elif not question and owner.candidates:
            if value.act == "deny":
                action = ActionDraft(capability=f"{prefix}.refine", arguments={**args, "reject_candidates": True})
            elif len(owner.candidates) == 1:
                action = ActionDraft(
                    capability=f"{prefix}.select", arguments={**args, "place_handle": owner.candidates[0]["handle"]}
                )
        if action:
            value = SemanticTurn(act="request", actions=[action])
    if value.act in {"confirm", "deny"} and current is None and state.navigation_focus == "navigation":
        nav = state.navigation
        candidate = (nav.pending or {}).get("navigation") or nav.model_dump(mode="json")
        if (candidate.get("offer") or {}).get("status") == "pending" and not candidate.get("clarification"):
            value = SemanticTurn(
                act="request",
                actions=[
                    ActionDraft(
                        capability="navigation.start" if value.act == "confirm" else "navigation.decline",
                        arguments={"route_revision": nav.revision},
                    )
                ],
            )
    # All tiers, including typed UI, validate references before changing a task.
    for draft in value.drafts():
        if not draft.capability.startswith(("trip.", "navigation.")):
            continue
        owner = state.trip_plan if draft.capability.startswith("trip.") else state.navigation
        args = draft.arguments
        if (
            draft.capability.startswith("trip.")
            and draft.capability != "trip.plan"
            and (not owner or args.plan_id != owner.id or args.route_revision != owner.revision)
        ):
            return state, "Kế hoạch đã thay đổi. Bạn chọn lại nhé."
        if args.choice_handle and (
            not owner
            or not any(
                choice["handle"] == args.choice_handle for choice in (owner.clarification or {}).get("choices", [])
            )
        ):
            return state, "Lựa chọn đã thay đổi. Bạn chọn lại nhé."
        if args.place_handle:
            energy = (
                owner.pending.get("navigation", {}).get("energy")
                if owner and owner.pending
                else owner.energy
                if owner
                else None
            )
            places = [*owner.candidates, *(energy or {}).get("station_candidates", [])] if owner else []
            if not any(place.get("handle") == args.place_handle for place in places):
                return state, "Địa điểm đã thay đổi. Bạn chọn lại nhé."
    plan = state.trip_plan
    if (
        plan
        and state.navigation_focus == "trip"
        and plan.offer
        and plan.offer.get("status") == "pending"
        and not plan.clarification
        and value.act in {"confirm", "deny"}
        and (not state.active or state.active.current_index() is None)
    ):
        value = SemanticTurn(
            act="request",
            actions=[
                ActionDraft(
                    capability="trip.start" if value.act == "confirm" else "trip.decline",
                    arguments={"plan_id": plan.id, "route_revision": plan.revision},
                )
            ],
        )
    # A selection is a slot answer to the existing route step, including UI
    # events. Keep the task and its remaining steps instead of replacing them.
    current_index = state.active.current_index() if state.active else None
    if value.act == "request" and len(value.actions) == 1 and current_index is not None:
        current = state.active.steps[current_index]
        incoming = value.actions[0]
        if incoming.capability in {"navigation.cancel", "trip.cancel"}:
            owner = state.trip_plan if incoming.capability.startswith("trip.") else state.navigation
            if (
                not owner
                or incoming.arguments.route_revision != owner.revision
                or incoming.capability.startswith("trip.")
                and incoming.arguments.plan_id != owner.id
            ):
                return state, "Yêu cầu đã thay đổi. Bạn chọn lại nhé."
            if current.draft.capability.split(".")[0] == incoming.capability.split(".")[0]:
                prefix = incoming.capability.split(".")[0] + "."
                for item in state.active.steps:
                    if item.draft.capability.startswith(prefix) and item.status not in {"completed", "unknown", "running"}:
                        item.status, item.confirmation = "cancelled", None
                state.active.revision += 1
                if prefix == "navigation.":
                    nav = state.navigation
                    nav.revision += 1
                    nav.pending, nav.offer, nav.clarification, nav.candidates = None, None, None, []
                    if nav.status in {"preview", "selecting"}:
                        nav.status, nav.route, nav.selected, nav.energy = "idle", None, None, None
                    state.navigation = nav
                elif state.trip_plan:
                    state.trip_plan.revision += 1
                    state.trip_plan.calculation_status = "cancelled"
                    state.trip_plan.offer, state.trip_plan.clarification = None, None
                return state, None if state.active.current_index() is not None else "Đã huỷ yêu cầu lập tuyến."
        if (
            incoming.capability in {"trip.select", "trip.refine", "trip.start"}
            and current.status == "waiting_slot"
            and current.draft.capability.startswith("trip.")
        ):
            value = SemanticTurn(
                act="modify", task_ref="active", step_updates=[{"step_index": current_index, "action": incoming}]
            )
        if (
            incoming.capability == "navigation.start"
            and current.status == "waiting_slot"
            and current.draft.capability in {"navigation.plan", "navigation.refine"}
        ):
            return state, "Bạn chọn địa điểm trước, rồi xác nhận tuyến để bắt đầu nhé."
        if (
            (
                incoming.capability in {"navigation.select", "navigation.refine"}
                or incoming.capability == "navigation.plan"
                and state.navigation.clarification
            )
            and current.status == "waiting_slot"
            and current.draft.capability in {"navigation.plan", "navigation.refine", "navigation.search"}
        ):
            if incoming.capability == "navigation.select" and not any(
                p["handle"] == incoming.arguments.place_handle for p in state.navigation.candidates
            ):
                return state, "Danh sách địa điểm đã thay đổi. Bạn chọn lại nhé."
            if (
                incoming.capability == "navigation.refine"
                and incoming.arguments.choice_handle
                and not any(
                    c["handle"] == incoming.arguments.choice_handle
                    for c in (state.navigation.clarification or {}).get("choices", [])
                )
            ):
                return state, "Lựa chọn đã thay đổi. Bạn chọn lại nhé."
            value = SemanticTurn(
                act="modify", task_ref="active", step_updates=[{"step_index": current_index, "action": incoming}]
            )
    if state.active and value.act in {"modify", "confirm", "deny", "cancel", "clarify"}:
        state.active.expires_at = datetime.now(UTC) + timedelta(minutes=3)
    if value.act == "clarify":
        if state.active and revoke_consent_on_clarify:
            # Unresolved input cannot leave consent for an older interpretation
            # usable by a later voice reply or stale UI button.
            state, _ = reject_modification(state, state.active, "")
        state.clarification = ClarificationState(
            source_text=value.text, question=value.text, unresolved=value.unresolved
        )
    if value.act == "unsupported":
        return state, value.text or "Mình chưa hỗ trợ chức năng đó bằng giọng nói."
    if value.negated and value.act == "reply" and state.active and state.active.current_index() is not None:
        cancelled, _ = reduce_turn(state, SemanticTurn(act="cancel", task_ref="active"))
        return cancelled, value.text or "Mình đã huỷ các bước chưa thực hiện."
    if value.negated or value.conditional or value.scheduled or value.conflicting or value.unresolved:
        state.clarification = ClarificationState(
            source_text=value.text,
            question=value.text or "Bạn nói rõ phần chưa hiểu nhé.",
            drafts=value.clarification_drafts or value.drafts(),
            unresolved=value.unresolved,
        )
        return state, value.text or "Bạn nói rõ thao tác muốn thực hiện ngay nhé."
    if value.act in {"clarify", "unsupported", "reply"}:
        return state, value.text or "Bạn nói rõ yêu cầu nhé."
    if value.act in {"confirm", "deny"} and value.task_ref != "active":
        return state, "Mình đã bỏ qua yêu cầu đó." if value.act == "deny" else "Mình chưa có đề xuất đang chờ xác nhận."
    task = state.active if value.task_ref == "active" else None
    if value.task_ref and value.task_ref.startswith("suspended:"):
        index = int(value.task_ref[-1])
        task = state.suspended[index] if index < len(state.suspended) else None
        if task and value.act in {"resume", "modify"}:
            state.suspended.pop(index)
            if state.active:
                state.suspended.insert(0, state.active)
            state.active = task
    if value.act in {"modify", "confirm", "deny", "cancel", "resume", "suspend"} and task is None:
        return state, "Mình đã bỏ qua yêu cầu đó." if value.act in {
            "deny",
            "cancel",
        } else "Mình không có yêu cầu đang chờ phù hợp. Bạn nói lại thao tác nhé."
    if value.act == "suspend":
        task.revision += 1
        for step in task.steps:
            if step.status not in {"completed", "cancelled"}:
                step.confirmation = None
                if step.status not in {"unknown", "running"}:
                    step.status, step.command_id, step.resolved_arguments = "pending", None, None
        state.suspended.insert(0, task)
        state.suspended = state.suspended[:3]
        state.active = None
        return state, value.text or "Mình tạm giữ yêu cầu đó."
    if value.act in {"deny", "cancel"}:
        task.revision += 1
        for step in task.steps:
            if step.status != "completed":
                step.status = "cancelled"
                step.confirmation = None
        if any(s.draft.capability.startswith("trip.") for s in task.steps) and state.trip_plan:
            state.trip_plan.revision += 1
            state.trip_plan.calculation_status = "cancelled"
            state.trip_plan.offer, state.trip_plan.clarification = None, None
            state.trip_plan.candidates = []
        if any(s.draft.capability.startswith("navigation.") for s in task.steps):
            nav = state.navigation
            nav.clarification = None
            nav.candidates = []
            nav.pending = None
            nav.revision += 1
            if nav.status in {"preview", "selecting"}:
                nav.status = "idle"
                nav.selected, nav.route, nav.energy = None, None, None
                nav.summary, nav.alternatives = None, []
            state.navigation = nav
        return state, "Mình đã hủy các bước chưa thực hiện."
    # Bind deictic targets from server-owned state for every provider. A model
    # cannot invent or silently override a referenced side.
    if any(draft.referenced_target for draft in value.drafts()):
        source = task or state.active
        index = source.current_index() if source else None
        value = value.model_copy(deep=True)
        patch_targets = {id(patch.action): patch.step_index for patch in value.step_updates}
        if value.act == "modify":
            start = value.step_index if value.step_index is not None else index
            if start is not None:
                patch_targets.update({id(action): start + offset for offset, action in enumerate(value.actions)})
        for draft in value.drafts():
            if not draft.referenced_target:
                continue
            target_index = patch_targets.get(id(draft), index if index is not None else -1)
            previous = (
                source.steps[target_index].draft
                if source and source.steps and target_index < len(source.steps)
                else None
            )
            target = transferable_arguments(previous, draft) if previous else {}
            target = {key: val for key, val in target.items() if key in {"zone", "zones"}}
            explicit = {
                key: val
                for key, val in draft.arguments.model_dump(exclude_none=True).items()
                if key in {"zone", "zones"}
            }
            if not target or (explicit and explicit != target):
                return state, "Bạn nói rõ vị trí muốn thực hiện nhé."
            for key, val in target.items():
                setattr(draft.arguments, key, val)
    if value.act in {"request", "question", "offer"}:
        if not value.actions:
            return state, value.text or "Bạn muốn mình làm gì?"
        if value.act == "question" and state.active and state.active.current_index() is not None:
            paused, _ = reduce_turn(state, SemanticTurn(act="suspend", task_ref="active"))
            state = paused
        state.active = DialogueTask(
            contract_version=9,
            steps=[
                TaskStep(draft=draft, status="offer" if value.act == "offer" else "pending") for draft in value.actions
            ],
        )
        state.clarification = None
        return state, value.text if value.act == "offer" else None
    index = value.step_index if value.step_index is not None else task.current_index()
    if index is None or index >= len(task.steps):
        return state, "Yêu cầu đó đã hoàn tất."
    step = task.steps[index]
    if step.status in {"completed", "cancelled"}:
        return state, "Bước đó đã kết thúc; mình không gửi lại lệnh."
    if value.act == "modify":
        changes = (
            [(patch.step_index, patch.action) for patch in value.step_updates]
            if value.step_updates
            else list(enumerate(value.actions, start=index))
        )
        indices = [target for target, _ in changes]
        if (not changes and not value.append_actions) or any(i >= len(task.steps) for i in indices):
            return reject_modification(state, task, "Bạn muốn sửa bước nào của yêu cầu?")
        if len(task.steps) + len(value.append_actions) > 5:
            return reject_modification(
                state, task, "Mỗi yêu cầu hỗ trợ tối đa năm thao tác; bạn chia thành yêu cầu mới nhé."
            )
        # Resolve the entire ordered patch before mutating any step. A model
        # cannot rewrite completed effects or grant consent here. New operations
        # require explicit append_actions and still pass the normal scheduler.
        for target, incoming in changes:
            previous = task.steps[target]
            if previous.status in {"completed", "cancelled", "unknown", "running"}:
                return reject_modification(
                    state, task, "Bước trước đã kết thúc hoặc chưa được xác minh; mình không gửi lại lệnh."
                )
            if not value.step_updates and len(indices) > 1 and not compatible_change(previous.draft, incoming):
                return reject_modification(state, task, "Bạn muốn sửa bước nào của yêu cầu?")
        for target, incoming in changes:
            previous = task.steps[target]
            previous.draft = ActionDraft(
                capability=incoming.capability,
                arguments=merge_arguments(previous.draft, incoming),
                excluded_zones=merge_exclusions(previous.draft, incoming),
                referenced_target=False,
            )
            (
                previous.status,
                previous.confirmation,
                previous.command_id,
                previous.result,
                previous.resolved_arguments,
            ) = (
                "pending",
                None,
                None,
                None,
                None,
            )
            previous.missing_slots = []
        task.steps.extend(TaskStep(draft=draft) for draft in value.append_actions)
        task.revision += 1
        for target, other in enumerate(task.steps):
            if target not in indices and other.status in {"pending", "waiting_confirmation", "approved"}:
                other.status, other.confirmation, other.command_id, other.resolved_arguments = (
                    "pending",
                    None,
                    None,
                    None,
                )
        task.expires_at = datetime.now(UTC) + timedelta(minutes=3)
    elif value.act == "confirm":
        if step.status == "offer":
            step.status = "pending"
        elif step.status == "waiting_confirmation":
            if step.confirmation and datetime.fromisoformat(step.confirmation["expires_at"]) <= datetime.now(UTC):
                step.status, step.confirmation, step.command_id, step.resolved_arguments = "pending", None, None, None
                task.revision += 1
            else:
                step.status = "approved"
        else:
            return state, "Bước này còn thiếu thông tin, chưa thể xác nhận."
    elif value.act == "resume":
        if step.status in {"blocked", "unknown", "running"}:
            return state, "Bước trước chưa được xác minh hoặc bị chặn. Bạn kiểm tra và tạo lại yêu cầu nhé."
        if step.status == "waiting_confirmation":
            step.confirmation, step.command_id, step.resolved_arguments = None, None, None
            task.revision += 1
        step.status = "pending"
    return state, None


def record_step(
    session: SessionState,
    index: int,
    *,
    status: str,
    result: dict | None = None,
    confirmation: dict | None = None,
    command_id: str | None = None,
):
    state = session.model_copy(deep=True)
    step = state.active.steps[index]
    step.status, step.result, step.confirmation = status, result, confirmation
    step.command_id = command_id or step.command_id
    return state


def prepare_step(session: SessionState, index: int, arguments: dict, command_id: str):
    state = session.model_copy(deep=True)
    step = state.active.steps[index]
    step.resolved_arguments, step.command_id, step.status = arguments, command_id, "running"
    return state


def record_navigation(session, update):
    from src.vivi.dialogue.contracts import NavigationState

    state = session.model_copy(deep=True)
    nav = NavigationState.model_validate(update)
    nav.event_sequence = session.navigation.event_sequence + 1
    state.navigation = nav
    if nav.status in {"ended", "completed"} and session.navigation.status not in {"ended", "completed"}:
        state.navigation_workspace.history = [
            *state.navigation_workspace.history[-19:],
            {
                "id": nav.route_id,
                "destination": nav.selected,
                "status": nav.status,
                "distance_m": nav.progress_m,
                "ended_at": datetime.now(UTC).isoformat(),
            },
        ]
    before, after = session.navigation_workspace.draft, state.navigation_workspace.draft
    if after and (not before or before.request_id != after.request_id or before.revision != after.revision):
        state.navigation_focus = "navigation"
    return state


def record_trip_plan(session, update):
    from src.vivi.dialogue.contracts import TripPlanState

    state = session.model_copy(deep=True)
    state.navigation_focus = "trip"
    state.trip_plan = TripPlanState.model_validate(update)
    state.trip_plan.event_sequence = (session.trip_plan.event_sequence if session.trip_plan else 0) + 1
    return state


def clear_journey(session):
    """Clear journey references while retaining unrelated tasks and effect history."""
    state = session.model_copy(deep=True)
    state.trip_plan = None
    for task in [state.active, *state.suspended]:
        if task:
            remaining = [step for step in task.steps if not step.draft.capability.startswith(("navigation.", "trip."))]
            if len(remaining) != len(task.steps):
                task.steps = remaining
                task.revision += 1
                for step in task.steps:
                    if step.status in {"waiting_confirmation", "approved"}:
                        step.status, step.confirmation, step.command_id, step.resolved_arguments = (
                            "pending",
                            None,
                            None,
                            None,
                        )
    if state.active and not state.active.steps:
        state.active = None
    state.suspended = [task for task in state.suspended if task.current_index() is not None]
    if state.clarification and any(
        draft.capability.startswith(("navigation.", "trip.")) for draft in state.clarification.drafts
    ):
        state.clarification = None
    return state


def upgrade_contract(session):
    state = session.model_copy(deep=True)
    for task in [state.active, *state.suspended]:
        if task and task.contract_version != 9:
            task.contract_version = 9
            task.revision += 1
            for step in task.steps:
                if (
                    step.draft.capability.startswith("navigation.")
                    and state.navigation.trip_kind is None
                    and step.status not in {"completed", "cancelled"}
                ):
                    step.status = "blocked"
                    step.confirmation, step.command_id, step.resolved_arguments = None, None, None
                    step.result = {
                        "status": "blocked",
                        "error_code": "trip_kind_required",
                        "message": "Kế hoạch cũ chưa rõ một chiều hay khứ hồi. Bạn lập lại tuyến nhé.",
                    }
                if step.status in {"waiting_confirmation", "approved"}:
                    if (
                        task is state.active
                        and step.draft.capability == "navigation.plan"
                        and state.navigation.status == "preview"
                    ):
                        # Older unique-match routes skipped the destination
                        # choice. Require it now without replaying any effects.
                        nav = state.navigation
                        nav.status = "selecting"
                        nav.selected, nav.route = None, None
                        nav.energy, nav.alternatives = None, []
                        state.navigation = nav
                    step.status, step.confirmation, step.command_id, step.resolved_arguments = (
                        "pending",
                        None,
                        None,
                        None,
                    )
    return state
