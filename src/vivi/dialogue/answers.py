"""Decode a typed answer to one persisted, revision-bound question."""

from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError

from src.vivi.dialogue.contracts import SemanticTurn


class AnswerResolver:
    @staticmethod
    def effect(action, question=None):
        if not action:
            return None
        modifying = question and question.slot_effect and question.target_id == question.step_id
        return SemanticTurn(
            act="modify" if modifying else "request",
            task_ref="active" if modifying else None,
            actions=[action.model_copy(deep=True)],
            consent_question_id=question.id if question and question.purpose == "approve_execution" else None,
        )

    def resolve(self, text, session, *, reply=None, act=None, literal=False):
        question = session.dialogue_question
        if question is None:
            return (
                SemanticTurn(act="clarify", text="Câu hỏi không còn hiệu lực. Bạn yêu cầu lại nhé.") if reply else None
            )
        if question.expires_at <= datetime.now(UTC):
            return SemanticTurn(act="clarify", text="Câu hỏi đã hết hạn. Bạn xác nhận lại yêu cầu nhé.")
        if question.step_id and question.target_id == question.step_id:
            task = session.active
            step = next((s for s in task.steps if s.id == question.step_id), None) if task else None
            valid = task and task.id == question.task_id and task.revision == question.revision and step
        else:
            plan = session.navigation_workspace.draft
            valid = plan and plan.id == question.target_id and plan.revision == question.revision
            if question.task_id:
                valid = valid and session.active and session.active.id == question.task_id
                step = (
                    next((s for s in session.active.steps if s.id == question.step_id), None)
                    if session.active
                    else None
                )
                valid = (
                    valid
                    and step
                    and step.draft.capability.split(".")[0] == question.owner
                    and step.status == "waiting_slot"
                )
        if not valid:
            return SemanticTurn(act="clarify", text="Trạng thái của câu hỏi đã thay đổi. Bạn chọn lại nhé.")
        if reply and reply.question_id != question.id:
            return SemanticTurn(act="clarify", text="Lựa chọn không còn thuộc câu hỏi hiện tại.")
        if reply and reply.value is not None:
            if question.purpose != "collect_slot":
                return SemanticTurn(act="clarify", text="Bạn chọn một lựa chọn của câu hỏi này nhé.")
            if question.slot in {"area", "name"}:
                from src.vivi.navigation.contracts import DestinationSpec

                destination = question.destination
                if question.slot == "area" and destination:
                    destination = destination.with_area(reply.value, question_id=question.id)
                else:
                    destination = DestinationSpec(
                        raw_text=reply.value, name=reply.value, area=destination.area if destination else None
                    )
                arguments = {"destination": destination, "query": destination.query()}
                if question.slot_effect and question.target_id == question.step_id:
                    effect = question.slot_effect.model_copy(deep=True)
                    effect.arguments.destination = destination
                    effect.arguments.query = destination.query()
                    return SemanticTurn(act="modify", task_ref="active", actions=[effect])
                if question.target_id != question.step_id:
                    arguments.update(plan_id=question.target_id, route_revision=question.revision)
                return SemanticTurn(
                    act="request", actions=[{"capability": f"{question.owner}.refine", "arguments": arguments}]
                )
            if question.slot_effect and question.slot:
                effect = question.slot_effect.model_copy(deep=True)
                field = type(effect.arguments).model_fields.get(question.slot)
                if field:
                    adapter = TypeAdapter(field.annotation)
                    try:
                        value = adapter.validate_python(reply.value)
                    except ValidationError:
                        try:
                            value = adapter.validate_json(reply.value)
                        except ValidationError:
                            return SemanticTurn(act="clarify", text="Giá trị chưa phù hợp với thông tin đang hỏi.")
                    setattr(effect.arguments, question.slot, value)
                    return SemanticTurn(act="modify", task_ref="active", actions=[effect])
            return SemanticTurn(act="clarify", text="Câu hỏi này cần thêm thông tin.")
        option_id = reply.option_id if reply else "yes" if act == "confirm" else "no" if act == "deny" else None
        if option_id in {"yes", "no"}:
            yes = option_id == "yes"
            effect = question.yes_effect if yes else question.no_effect
            if effect:
                return self.effect(effect, question)
            answer_act = question.yes_act if yes else question.no_act
            if answer_act:
                return SemanticTurn(act=answer_act, task_ref="active", consent_question_id=question.id if yes else None)
            return SemanticTurn(act="clarify", text=question.text)
        option = next((o for o in question.options if o.id == option_id), None)
        return (
            self.effect(option.effect, question)
            if option
            else (SemanticTurn(act="clarify", text="Lựa chọn không hợp lệ cho câu hỏi này.") if reply else None)
        )
