"""The sole writer of checkpoint-owned task state; no I/O or language matching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.vivi.capabilities.registry import (
    REGISTRY,
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


def synchronize_question(session, proposal=None):
    """The reducer owns the persisted foreground question, including legacy import."""
    from src.vivi.dialogue.questions import bind_question

    session.dialogue_question = bind_question(session, proposal)
    if (
        session.dialogue_question
        and session.dialogue_question.expires_at <= datetime.now(UTC)
        and session.dialogue_question.kind != "confirm_action"
    ):
        from uuid import uuid4

        session.dialogue_question = session.dialogue_question.model_copy(
            update={
                "id": "question:" + str(uuid4()),
                "expires_at": datetime.now(UTC) + timedelta(minutes=3),
            }
        )
    return session


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
    if value.act in {"confirm", "deny"}:
        from src.vivi.dialogue.answers import AnswerResolver

        answer = AnswerResolver().resolve("", state, act=value.act)
        if answer:
            value = answer
    # A spoken place answers the question even when a model also echoed a
    # choice handle that the previous answer already consumed ("Đồ Sơn").
    stale_choices = [
        draft
        for draft in value.drafts()
        if draft.capability == "navigation.refine"
        and draft.arguments.choice_handle
        and (draft.arguments.query or draft.arguments.destination)
        and not any(
            choice["handle"] == draft.arguments.choice_handle
            for choice in (getattr(state.navigation_workspace.draft, "clarification", None) or {}).get("choices", [])
        )
    ]
    if stale_choices:
        value = value.model_copy(deep=True)
        for draft in value.drafts():
            if draft.capability == "navigation.refine" and draft.arguments.choice_handle in {
                stale.arguments.choice_handle for stale in stale_choices
            }:
                draft.arguments.choice_handle = None
    # All tiers, including typed UI, validate references before changing a task.
    for draft in value.drafts():
        if not draft.capability.startswith("navigation."):
            continue
        owner = state.navigation_workspace.draft
        args = draft.arguments
        if (
            args.plan_id
            and (not owner or args.plan_id != owner.id)
            or args.route_revision
            and (not owner or args.route_revision != owner.revision)
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
            energy = owner.energy if owner else None
            places = [*owner.candidates, *(energy or {}).get("station_candidates", [])] if owner else []
            if not any(place.get("handle") == args.place_handle for place in places):
                return state, "Địa điểm đã thay đổi. Bạn chọn lại nhé."
    # A selection is a slot answer to the existing route step, including UI
    # events. Keep the task and its remaining steps instead of replacing them.
    current_index = state.active.current_index() if state.active else None
    if value.act == "request" and len(value.actions) == 1 and current_index is not None:
        current = state.active.steps[current_index]
        incoming = value.actions[0]
        if (
            incoming.capability == "navigation.start"
            and current.draft.capability == "navigation.start"
            and current.status == "waiting_confirmation"
        ):
            expected = current.draft.arguments
            if (
                incoming.arguments.plan_id != expected.plan_id
                or incoming.arguments.route_revision != expected.route_revision
            ):
                return state, "Kế hoạch đã thay đổi. Bạn xác nhận lại nhé."
            value = SemanticTurn(act="confirm", task_ref="active", consent_question_id=value.consent_question_id)
        if incoming.capability == "navigation.cancel":
            owner = state.navigation_workspace.draft
            if (
                not owner
                or incoming.arguments.route_revision != owner.revision
                or incoming.arguments.plan_id != owner.id
            ):
                return state, "Yêu cầu đã thay đổi. Bạn chọn lại nhé."
            if current.draft.capability.split(".")[0] == incoming.capability.split(".")[0]:
                prefix = incoming.capability.split(".")[0] + "."
                whole_task = value.task_ref == "active"
                for item in state.active.steps:
                    if (whole_task or item.draft.capability.startswith(prefix)) and item.status not in {
                        "completed",
                        "unknown",
                        "running",
                    }:
                        item.status, item.confirmation = "cancelled", None
                state.active.revision += 1
                if not whole_task:
                    # A component cancel (e.g. the trip UI) keeps independently
                    # requested steps. A bare voice cancel targets the whole task.
                    state.domain_commands.append(
                        {
                            "kind": "cancel_draft",
                            "plan_id": state.navigation_workspace.draft.id
                            if state.navigation_workspace.draft
                            else None,
                        }
                    )
                    return state, None if state.active.current_index() is not None else "Đã huỷ yêu cầu lập tuyến."
                # Schedule the whole-task cancellation through the domain writer
                # to revoke the saved route proposal without running later steps.
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
    if value.act in {"deny", "cancel"} and task is None and state.navigation_workspace.open_draft:
        # The plan outlived its task (e.g. after a misheard turn); still cancel it.
        state.domain_commands.append({"kind": "cancel_draft", "plan_id": state.navigation_workspace.open_draft.id})
        return state, "Đã hủy kế hoạch đang chờ."
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
        if any(s.draft.capability.startswith("navigation.") for s in task.steps):
            state.domain_commands.append(
                {
                    "kind": "cancel_draft",
                    "plan_id": state.navigation_workspace.draft.id if state.navigation_workspace.draft else None,
                }
            )
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
        information = all(
            REGISTRY[a.capability].spec.read_only and REGISTRY[a.capability].executor != "navigation"
            for a in value.actions
        )
        if (value.act == "question" or information) and state.active and state.active.current_index() is not None:
            paused, _ = reduce_turn(state, SemanticTurn(act="suspend", task_ref="active"))
            state = paused
        state.active = DialogueTask(
            contract_version=10,
            steps=[
                TaskStep(
                    draft=draft,
                    status="offer"
                    if value.act == "offer"
                    else "approved"
                    if draft.capability == "navigation.start"
                    and value.consent_question_id
                    and state.dialogue_question
                    and value.consent_question_id == state.dialogue_question.id
                    else "waiting_confirmation"
                    if draft.capability == "navigation.start"
                    else "pending",
                )
                for draft in value.actions
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
                question = state.dialogue_question
                if not question or value.consent_question_id != question.id:
                    return state, "Bạn trả lời câu hỏi xác nhận hiện tại trước khi thực hiện nhé."
                step.status = "approved"
        else:
            return state, "Bước này còn thiếu thông tin, chưa thể xác nhận."
    elif value.act == "resume":
        if step.status in {"blocked", "unknown", "running"}:
            return state, "Bước trước chưa được xác minh hoặc bị chặn. Bạn kiểm tra và tạo lại yêu cầu nhé."
        if step.status == "waiting_confirmation":
            step.confirmation, step.command_id, step.resolved_arguments = None, None, None
            task.revision += 1
        step.status = "waiting_confirmation" if step.draft.capability == "navigation.start" else "pending"
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


def clear_journey(session):
    """Clear journey references while retaining unrelated tasks and effect history."""
    state = session.model_copy(deep=True)
    for task in [state.active, *state.suspended]:
        if task:
            remaining = [step for step in task.steps if not step.draft.capability.startswith("navigation.")]
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
    if state.clarification and any(draft.capability.startswith("navigation.") for draft in state.clarification.drafts):
        state.clarification = None
    return state


def synchronize_navigation_question(session):
    """A changed plan revokes a pending start; it never inherits old approval."""
    task = session.active
    index = task.current_index() if task else None
    plan = session.navigation_workspace.draft
    if index is not None and plan:
        step = task.steps[index]
        if step.draft.capability == "navigation.start" and step.status not in {
            "unknown",
            "running",
            "completed",
            "cancelled",
        }:
            args = step.draft.arguments
            if args.plan_id and args.plan_id != plan.id:
                step.status = "blocked"
                step.result = {
                    "status": "blocked",
                    "message": "Kế hoạch này đã được thay thế. Bạn lập lại tuyến nhé.",
                    "error_code": "stale_plan",
                }
                session.dialogue_question = None
            elif args.route_revision != plan.revision:
                args.plan_id, args.route_revision = plan.id, plan.revision
                step.status, step.confirmation, step.command_id, step.resolved_arguments = (
                    "waiting_confirmation",
                    None,
                    None,
                    None,
                )
                task.revision += 1
                session.dialogue_question = None
    return synchronize_question(session)
