"""One boundary for spoken and typed answers to the current question."""

import re
from datetime import UTC, datetime

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn, SessionState
from src.vivi.dialogue.parser import Lexer, OfflineParser, affirmed
from src.vivi.dialogue.questions import current_question
from src.vivi.navigation.contracts import AreaConstraint, DestinationSpec
from src.vivi.navigation.destinations import administrative_layer, center_requested, region_for
from src.vivi.text import normalize_text


class AnswerResolver:
    @staticmethod
    def effect(action, question=None):
        if not action:
            return None
        modifying = question and question.slot_effect and question.target_id == question.step_id
        return SemanticTurn(act="modify" if modifying else "request", task_ref="active" if modifying else None,
                            actions=[action.model_copy(deep=True)])

    def resolve(self, text, session, *, reply=None, act=None, literal=False):
        question = current_question(session)
        if question is None:
            return SemanticTurn(act="clarify", text="Câu hỏi không còn hiệu lực. Bạn yêu cầu lại nhé.") if reply else None
        if reply:
            if reply.question_id != question.id:
                return SemanticTurn(act="clarify", text="Lựa chọn không còn thuộc câu hỏi hiện tại.")
            if reply.value is not None:
                if question.expires_at <= datetime.now(UTC):
                    return SemanticTurn(act="clarify", text="Câu hỏi đã hết hạn. Bạn yêu cầu lại nhé.")
                return self.resolve(reply.value, session, literal=True) or SemanticTurn(
                    act="clarify", text="Câu trả lời chưa đủ rõ cho câu hỏi này.")
            option = next((o for o in question.options if o.id == reply.option_id), None)
            if reply.option_id in {"yes", "no"}:
                if question.expires_at <= datetime.now(UTC):
                    return SemanticTurn(act="clarify", text="Câu hỏi đã hết hạn. Bạn yêu cầu lại nhé.")
                return self.resolve("", session, act="confirm" if reply.option_id == "yes" else "deny") or SemanticTurn(
                    act="clarify", text="Câu hỏi này cần thêm thông tin, bạn bổ sung nhé.")
            if not option:
                return SemanticTurn(act="clarify", text="Lựa chọn không hợp lệ cho câu hỏi này.")
            if question.expires_at <= datetime.now(UTC):
                return SemanticTurn(act="clarify", text="Câu hỏi đã hết hạn. Bạn yêu cầu lại nhé.")
            return self.effect(option.effect, question)
        value = Lexer().normalize(text)
        if literal and question.purpose == "collect_slot" and question.slot_effect and question.slot not in {"name", "area"}:
            from pydantic import TypeAdapter, ValidationError

            effect = question.slot_effect.model_copy(deep=True)
            field = type(effect.arguments).model_fields.get(question.slot)
            if field:
                try:
                    setattr(effect.arguments, question.slot, TypeAdapter(field.annotation).validate_python(text))
                except ValidationError:
                    try:
                        setattr(effect.arguments, question.slot, TypeAdapter(field.annotation).validate_json(text))
                    except ValidationError:
                        return SemanticTurn(act="clarify", text="Giá trị chưa phù hợp với thông tin đang hỏi.")
                return SemanticTurn(act="modify", task_ref="active", actions=[effect])
        yes = not literal and (act == "confirm" or act is None and (
            affirmed(value) or question.kind == "accept_place" and value == "dung"
            or question.kind in {"start_plan", "accept_place", "choose_place"} and OfflineParser().start_consent(text, explicit=True)
        ))
        no = not literal and (act == "deny" or act is None and value in {"khong", "khong can", "khong dung", "chua", "chua di", "de sau"})
        if not literal and question.kind in {"accept_place", "choose_place"} and value in {
            "khong phai", "khong phai cho nay", "khong dung cho nay", "cho khac", "khong cho nao",
        }:
            no = True
        if yes or no:
            if question.expires_at <= datetime.now(UTC):
                # Legacy task consent handles expired grants by refreshing them.
                if question.kind == "confirm_action":
                    return SemanticTurn(act="confirm" if yes else "deny", task_ref="active")
                return SemanticTurn(act="clarify", text="Câu hỏi đã hết hạn. Bạn yêu cầu lại nhé.")
            # An acknowledgment keeps an existing proposal even for the legacy
            # negative wording. Literal yes/no and typed acts retain its polarity.
            if question.kind == "change_destination" and act is None and value not in {"co", "khong", "khong can"}:
                option = next((o for o in question.options if o.role == "representative"), None)
                return self.effect(option.effect, question) if option else None
            effect = question.yes_effect if yes else question.no_effect
            if effect:
                return self.effect(effect, question)
            answer_act = question.yes_act if yes else question.no_act
            if answer_act:
                return SemanticTurn(act=answer_act, task_ref="active")
            if question.options:
                return SemanticTurn(act="clarify", text=question.text)
            return None
        if question.expires_at <= datetime.now(UTC):
            return None
        if not literal and question.purpose == "collect_slot" and question.slot_effect and question.slot not in {"name", "area"}:
            patch = OfflineParser().slot_patch(value, question.slot_effect, text)
            if (patch and patch.capability == question.slot_effect.capability
                and getattr(patch.arguments, question.slot or "", None) is not None):
                effect = question.slot_effect.model_copy(deep=True)
                arguments = {**effect.arguments.model_dump(exclude_none=True), **patch.arguments.model_dump(exclude_none=True)}
                effect.arguments = type(effect.arguments).model_validate(arguments)
                return SemanticTurn(act="modify", task_ref="active", actions=[effect])
        if question.kind in {"confirm_destination", "change_destination", "scope"}:
            kind = self.choice_role(text, question)
            if kind:
                option = next((o for o in question.options if o.role == kind), None)
                return self.effect(option.effect, question) if option else None
        if question.slot == "area" and question.destination:
            area = re.sub(r"^(?:ở|o|tại|tai)\s+", "", OfflineParser.place_query(text), flags=re.I)
            if literal or region_for(area) or administrative_layer(area):
                destination = question.destination.with_area(area, question_id=question.id)
                arguments = {"query": destination.query(), "destination": destination}
                if question.owner == "trip":
                    arguments.update(plan_id=question.target_id, route_revision=question.revision)
                return self.effect(ActionDraft(capability=f"{question.owner}.refine", arguments=arguments), question)
        if question.slot == "name" and question.slot_effect:
            if not literal and value in {"chi", "anh", "em", "toi", "minh", "den", "toi do", "chi duong"}:
                return SemanticTurn(act="clarify", text=question.text)
            # Slot values are data; fully specified new intents retain routing.
            correction = OfflineParser.destination_correction(text) if not literal else None
            slot_text = correction or text
            bounded = OfflineParser().interpret(slot_text, SessionState())
            unrelated = any(not a.capability.startswith(("navigation.", "trip.")) for a in bounded.actions)
            intent_cue = re.match(r"^(?:mo|dong|dat|bat|tat|phat|tim|ghi nho|thoi tiet|gia vang)\b", Lexer().normalize(slot_text))
            if (literal or not OfflineParser.journey_arguments(text) and not (unrelated and (bounded.act == "question" or intent_cue))
                and not (bounded.conditional or bounded.scheduled or bounded.negated)
                and not re.match(r"^(?:khong|huy|thoi|dung)\b", value)
                and len(text) <= 200 and not re.search(r"\b(?:la gi|o dau|tai sao|neu|ngay mai)\b", value)):
                name = OfflineParser.place_query(slot_text)
                region = region_for(name)
                area = question.destination.area if question.destination else None
                if not area and question.destination:
                    previous_region = region_for(question.destination.name)
                    if question.destination.selection == "region" or previous_region:
                        area = AreaConstraint(text=previous_region["name"] if previous_region else question.destination.name, source="context")
                destination = DestinationSpec.from_arguments({"query": name})
                if destination.area:
                    destination = destination.with_area(destination.area.text, question_id=question.id)
                elif not correction and not (region or administrative_layer(name)):
                    destination.area = area
                destination.raw_text = text
                destination.selection = "region" if region else "named_place"
                effect = question.slot_effect.model_copy(deep=True)
                effect.arguments.destination, effect.arguments.query = destination, destination.query()
                return (SemanticTurn(act="modify", task_ref="active", actions=[effect]) if question.slot_effect.capability.endswith("plan")
                        else self.effect(effect, question))
        ordinal = OfflineParser.place_ordinal(value)
        if ordinal and question.options:
            token = ordinal[1]
            index = {"dau tien": 0, "mot": 0, "nhat": 0, "hai": 1, "ba": 2, "bon": 3, "tu": 3, "nam": 4}.get(token, int(token) - 1 if token.isdigit() else -1)
            return self.effect(question.options[index].effect, question) if 0 <= index < len(question.options) else SemanticTurn(
                act="clarify", text="Số đó không có trong danh sách hiện tại.")
        matches = [o for o in question.options if value == normalize_text(o.label) or value == normalize_text(o.label.split(",")[0])]
        if len(matches) == 1:
            return self.effect(matches[0].effect, question)
        if len(matches) > 1:
            return SemanticTurn(act="clarify", text="Có nhiều địa điểm trùng tên. Bạn chọn số thứ tự hoặc thêm địa chỉ nhé.")
        return None

    @staticmethod
    def choice_role(text, question):
        value = Lexer().normalize(text)
        if re.fullmatch(
            r"(?:(?:cu |van )?(?:giu|lay|chon)(?: nguyen)? (?:dia diem|diem(?: mac dinh| den| nay| do)?|trung tam)"
            r"(?: nay| do| mac dinh| da chon| hien tai)?|giu nguyen|khong doi|trung tam|diem dai dien)", value,
        ):
            return "representative"
        if re.fullmatch(
            r"(?:co[, ]+)?(?:doi(?: (?:dia diem|diem den|diem|cho))?|chon (?:dia diem|cho) khac|(?:mot )?(?:cho|noi|dia diem)(?: cu the)? khac|khong phai trung tam)", value,
        ):
            return "specific"
        if value in {"khong", "khong can"}:
            effect = question.no_effect
        elif value == "co":
            effect = question.yes_effect
        elif affirmed(value):
            return "representative"
        else:
            answer = re.sub(r"^(?:den |chon |di den )", "", value)
            destination = question.destination
            expected = region_for(destination.area.text if destination.area else destination.name) if destination else None
            actual = region_for(answer) if center_requested(answer) else None
            return "representative" if actual and expected and actual == expected else None
        return next((o.role for o in question.options if o.effect == effect), None)
