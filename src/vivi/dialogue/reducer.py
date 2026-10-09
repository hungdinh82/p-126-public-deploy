"""The sole writer of checkpoint-owned task state; no I/O or language matching."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.vivi.capabilities.registry import REGISTRY, transferable_arguments
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep


def expire(session: SessionState):
    state = session.model_copy(deep=True)
    state.suspended = [task for task in state.suspended if task.expires_at > datetime.now(UTC)]
    if state.active and state.active.expires_at <= datetime.now(UTC):
        state.active = None
    return state


def reduce_turn(session: SessionState, value: SemanticTurn) -> tuple[SessionState, str | None]:
    state = expire(session)
    if value.negated and value.act == "reply" and state.active and state.active.current_index() is not None:
        cancelled, _ = reduce_turn(state, SemanticTurn(act="cancel", task_ref="active"))
        return cancelled, value.text or "Mình đã huỷ các bước chưa thực hiện."
    if value.negated or value.conditional or value.scheduled or value.conflicting or value.unresolved:
        return state, value.text or "Bạn nói rõ thao tác muốn thực hiện ngay nhé."
    if value.act in {"clarify", "unsupported", "reply"}:
        if value.act == "reply" and state.active and any(s.status == "offer" for s in state.active.steps):
            for step in state.active.steps:
                if step.status == "offer":
                    step.status = "cancelled"
        return state, value.text or "Bạn nói rõ yêu cầu nhé."
    if value.act in {"confirm", "deny"} and value.task_ref != "active":
        return state, "Bạn cần tiếp tục yêu cầu đó trước khi xác nhận."
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
        return state, "Mình không có yêu cầu đang chờ phù hợp. Bạn nói lại thao tác nhé."
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
        return state, "Mình đã hủy các bước chưa thực hiện."
    # Bind deictic targets from server-owned state for every provider. A model
    # cannot invent or silently override a referenced side.
    if any(draft.referenced_target for draft in value.actions):
        source = task or state.active
        index = source.current_index() if source else None
        previous = source.steps[index if index is not None else -1].draft if source and source.steps else None
        value = value.model_copy(deep=True)
        for draft in value.actions:
            if not draft.referenced_target:
                continue
            target = transferable_arguments(previous, draft) if previous else {}
            target = {key: val for key, val in target.items() if key in {"zone", "zones"}}
            explicit = {key: val for key, val in draft.arguments.model_dump(exclude_none=True).items() if key in {"zone", "zones"}}
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
        state.active = DialogueTask(steps=[TaskStep(draft=draft, status="offer" if value.act == "offer" else "pending") for draft in value.actions])
        return state, value.text if value.act == "offer" else None
    index = value.step_index if value.step_index is not None else task.current_index()
    if index is None or index >= len(task.steps):
        return state, "Yêu cầu đó đã hoàn tất."
    step = task.steps[index]
    if step.status in {"completed", "cancelled"}:
        return state, "Bước đó đã kết thúc; mình không gửi lại lệnh."
    if value.act == "modify":
        if len(value.actions) != 1:
            return state, "Bạn muốn sửa bước nào của yêu cầu?"
        if step.status in {"unknown", "running"}:
            return state, "Kết quả bước trước chưa rõ. Bạn kiểm tra trạng thái trước khi tạo yêu cầu mới nhé."
        incoming = value.actions[0]
        old = step.draft.arguments.model_dump(exclude_none=True)
        merged = dict(old) if incoming.capability == step.draft.capability else transferable_arguments(step.draft, incoming)
        updates = incoming.arguments.model_dump(exclude_none=True)
        if "zone" in updates:
            merged.pop("zones", None)
        if "zones" in updates:
            merged.pop("zone", None)
        merged.update(updates)
        step.draft = ActionDraft(capability=incoming.capability, arguments=merged,
                                excluded_zones=incoming.excluded_zones if incoming.excluded_zones is not None else step.draft.excluded_zones if REGISTRY[incoming.capability].transferable.intersection({"zone", "zones"}) else None, referenced_target=False)
        step.status, step.confirmation, step.command_id, step.result, step.resolved_arguments = "pending", None, None, None, None
        task.revision += 1
        for other in task.steps:
            if other is not step and other.status in {"pending", "waiting_confirmation", "approved"}:
                other.status, other.confirmation, other.command_id, other.resolved_arguments = "pending", None, None, None
        task.expires_at = datetime.now(UTC) + timedelta(minutes=3)
    elif value.act == "confirm":
        if step.status == "offer":
            step.status = "pending"
        elif step.status == "waiting_confirmation":
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


def record_step(session: SessionState, index: int, *, status: str, result: dict | None = None,
                confirmation: dict | None = None, command_id: str | None = None):
    state = session.model_copy(deep=True)
    step = state.active.steps[index]
    step.status, step.result, step.confirmation = status, result, confirmation
    step.command_id = command_id or step.command_id
    if confirmation:
        state.active.expires_at = datetime.fromisoformat(confirmation["expires_at"])
    return state


def prepare_step(session: SessionState, index: int, arguments: dict, command_id: str):
    state = session.model_copy(deep=True)
    step = state.active.steps[index]
    step.resolved_arguments, step.command_id, step.status = arguments, command_id, "running"
    return state
