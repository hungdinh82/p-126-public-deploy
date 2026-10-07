from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.prompts import CLASSIFIER_INSTRUCTION
from src.vivi.config import Settings
from src.vivi.structured_llm import StructuredChatClient, compact_history, openrouter_client
from src.vivi.text import normalize_text as _normalize


class IntentClassifier(ABC):
    @abstractmethod
    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        raise NotImplementedError

    async def aclassify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return self.classify(input_text, history)

    def classify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        del vehicle_state
        return self.classify(input_text, history)

    async def aclassify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        # Preserve the context-aware sync implementation for deterministic
        # classifiers. Model-backed classifiers override this with native I/O.
        return self.classify_with_context(input_text, history, vehicle_state)


class StructuredAPIIntentClassifier(IntentClassifier):
    """Intent classifier for llama.cpp and OpenAI-compatible APIs."""

    def __init__(self, client: StructuredChatClient) -> None:
        self.client = client

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return self.classify_with_context(input_text, history, None)

    def classify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        priority = priority_decision(input_text)
        if priority is None and _polite_control_request(_normalize(input_text)):
            priority = RulesIntentClassifier().classify_with_context(input_text, history, vehicle_state)
        if priority is not None:
            return priority
        prompt = self._prompt(input_text, history, vehicle_state)
        payload = self.client.generate_json(
            system=CLASSIFIER_INSTRUCTION,
            user=prompt,
            schema=IntentDecision.model_json_schema(),
            schema_name="vivi_intent_decision",
        )
        return IntentDecision.model_validate_json(payload)

    async def aclassify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return await self.aclassify_with_context(input_text, history, None)

    async def aclassify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        priority = priority_decision(input_text)
        if priority is None and _polite_control_request(_normalize(input_text)):
            priority = RulesIntentClassifier().classify_with_context(input_text, history, vehicle_state)
        if priority is not None:
            return priority
        prompt = self._prompt(input_text, history, vehicle_state)
        payload = await self.client.agenerate_json(
            system=CLASSIFIER_INSTRUCTION,
            user=prompt,
            schema=IntentDecision.model_json_schema(),
            schema_name="vivi_intent_decision",
        )
        return IntentDecision.model_validate_json(payload)

    @staticmethod
    def _prompt(
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> str:
        return (
            f"Lịch sử gần đây: {json.dumps(compact_history(history), ensure_ascii=False)}\n"
            f"Trạng thái xe hiện tại: {json.dumps(vehicle_state or {}, ensure_ascii=False)}\n"
            f"Transcript hiện tại: {input_text}"
        )


class LocalIntentClassifier(StructuredAPIIntentClassifier):
    def __init__(self, config: Settings) -> None:
        super().__init__(
            StructuredChatClient(
                base_url=config.local_llm_base_url,
                api_key=config.local_llm_api_key,
                model=config.local_llm_model,
                timeout_seconds=config.llm_timeout_seconds,
                max_tokens=config.local_llm_max_tokens,
            )
        )


class OpenAIIntentClassifier(StructuredAPIIntentClassifier):
    def __init__(self, config: Settings) -> None:
        super().__init__(
            StructuredChatClient(
                base_url=config.openai_base_url,
                api_key=config.openai_api_key,
                model=config.openai_model,
                timeout_seconds=config.llm_timeout_seconds,
                max_tokens=config.local_llm_max_tokens,
            )
        )


class OpenRouterIntentClassifier(StructuredAPIIntentClassifier):
    def __init__(self, config: Settings) -> None:
        super().__init__(openrouter_client(config))


def is_live_status_request(input_text: str) -> bool:
    """Read observations, while instructions/specifications still use the handbook."""
    text = _normalize(input_text)
    if _polite_control_request(text):
        return False
    if re.search(r"\b(cach|lam sao|huong dan|tai sao|vi sao|nhu nao|the nao|"
                 r"dung luong|thong so|khuyen cao|khuyen nghi|nen|tieu chuan|sdi|catl|toi da|"
                 r"thi|xu ly|ra sao|can lam gi|gap vat can|khi)\b", text):
        return False
    if re.search(r"\b(dat|tang|giam|ha|chinh|mo|dong|bat|tat|khoa)\b", text) and not re.search(
        r"\b(dang|hien tai|bay gio|da.*chua|co.*khong)\b", text
    ):
        return False
    if re.search(r"trang thai xe|tinh trang xe|quang duong con lai|con di duoc|"
                 r"di (?:them |duoc ).*(?:bao xa|bao nhieu|km)", text):
        return True
    if re.search(r"\bpin\b", text) and re.search(r"\b(con|hien tai|bay gio|luc nay|"
                                               r"tinh trang|trang thai|phan tram|muc pin|kiem tra|doc)\b", text):
        return True
    if re.search(r"nhiet do|dieu hoa", text) and re.search(r"dang|hien tai|bay gio|bao nhieu|may do", text):
        return not re.search(r"\b(dat|tang|giam|ha|chinh)\b", text)
    if "ap suat lop" in text and re.search(r"hien tai|bay gio|dang|kiem tra|tinh trang|doc|cho biet|bao nhieu", text):
        return True
    return bool(re.search(r"cua|ghe|nhac", text) and re.search(
        r"dang|hien tai|da.*chua|co.*(?:dong|mo|khoa).*khong", text
    ))


def _polite_control_request(text: str) -> bool:
    if not re.search(r"\b(mo|dong|khoa|dat|tang|giam|ha|bat|tat|phat|dung)\b", text):
        return False
    if re.search(r"\b(cach|lam sao|huong dan|tai sao|vi sao)\b", text):
        return False
    return bool(re.search(r"(?:ban|vivi).{0,20}(?:co the|giup)|giup (?:minh|toi)|"
                          r"^co the.{0,10}(?:mo|dong|khoa|dat|tang|giam|ha|bat|tat|phat)|"
                          r"^(?:mo|dong|khoa|dat|tang|giam|ha|bat|tat|phat|dung).*duoc khong", text))


def priority_decision(input_text: str) -> IntentDecision | None:
    """Protect common tool boundaries even when an SLM routes them incorrectly."""
    from src.vivi.rag.scope import scope_rejection_reason

    text = _normalize(input_text)
    if scope_rejection_reason(input_text) is None and re.search(
        r"khong (?:the )?(?:mo|dong|sac|khoa).*duoc|(?:mo|dong|sac|khoa).*khong duoc|"
        r"bi ket|khong hoat dong", text
    ):
        return IntentDecision(route="handbook", intent="manual.search", arguments={"query": input_text})
    if not re.search(r"\b(cach|lam sao|vi sao|tai sao|huong dan)\b", text) and re.search(
        r"\b(dung|khong)\s+(?:mo|dong|khoa|tang|giam|bat|tat|phat|ha)\b", text
    ):
        return IntentDecision(route="conversation", intent="conversation.respond",
                              response_text="Được nhé, mình sẽ không thực hiện thao tác đó.")
    if not re.search(r"\b(cach|huong dan|nhu nao|the nao|tai sao|vi sao)\b", text):
        controls = re.findall(r"\b(?:mo|dong|khoa|dat|chinh|phat|tat|suoi)\b.{0,25}?(?:cua|nhiet do|nhac|ghe)", text)
        if len(controls) > 1 and re.search(r"\bva\b|roi|dong thoi", text):
            return IntentDecision(route="clarify", intent="conversation.clarify", needs_clarification=True,
                                  clarification_question="Bạn muốn mình thực hiện thao tác nào trước?")
        if controls and re.search(r"\b(neu|lat nua|ti nua|ngay mai)\b", text):
            return IntentDecision(route="clarify", intent="conversation.clarify", needs_clarification=True,
                                  clarification_question="Mình chưa hẹn giờ thao tác được. Bạn muốn thực hiện ngay không?")
    if is_live_status_request(input_text) and scope_rejection_reason(input_text) is None:
        return IntentDecision(route="action", intent="vehicle.get_status",
                              response_text="Mình kiểm tra nhé.")
    return None


class RulesIntentClassifier(IntentClassifier):
    """Deterministic offline classifier used by tests and degraded mode."""

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return self.classify_with_context(input_text, history, None)

    def classify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        text = _normalize(input_text).strip()
        priority = priority_decision(input_text)
        if priority is not None:
            return priority
        if re.fullmatch(r"(?:huong dan (?:su dung )?xe|(?:bo )?cam nang(?: co (?:gi|nhung thong tin gi))?)[.?! ]*", text):
            return IntentDecision(route="clarify", intent="conversation.clarify", needs_clarification=True,
                                  clarification_question="Bạn muốn tìm hiểu về sạc pin, tiện ích cabin hay hỗ trợ lái?")
        if re.search(r"ban la xe gi|vivi la xe gi|xe nay la xe gi", text):
            return IntentDecision(route="conversation", intent="conversation.respond",
                                  response_text="Mình là ViVi trên chiếc VF8 đang đồng hành cùng bạn.")
        if re.search(r"tam biet|hen gap lai", text):
            return IntentDecision(route="conversation", intent="conversation.respond", response_text="Hẹn gặp lại bạn nhé.")
        if re.search(r"ban (?:co )?khoe|vivi (?:co )?khoe", text):
            return IntentDecision(route="conversation", intent="conversation.respond",
                                  response_text="Mình luôn sẵn sàng đồng hành cùng bạn nhé.")
        if re.search(r"\b(?:toi|minh)\b.*met|met qua|buon ngu", text):
            return IntentDecision(route="conversation", intent="conversation.respond",
                                  response_text="Nếu bạn mệt, hãy tìm chỗ an toàn để nghỉ một chút nhé.")
        if re.search(r"\b(?:toi|minh)\b.*buon|chan qua", text):
            return IntentDecision(route="conversation", intent="conversation.respond",
                                  response_text="Mình ở đây với bạn. Bạn muốn nghe nhạc một chút không?")
        if re.search(r"sai roi|ban khong hieu|vivi khong hieu", text):
            return IntentDecision(route="conversation", intent="conversation.respond",
                                  response_text="Mình hiểu chưa đúng rồi. Bạn nói lại điều muốn mình làm nhé.")
        # Knowledge questions about physical controls must not become commands.
        # Run this before both prohibited-command matching and cabin parsers.
        from src.vivi.rag.scope import scope_rejection_reason

        knowledge_question = bool(re.search(
            r"\b(cach|lam sao|huong dan|tai sao|vi sao|the nao|la gi|bao nhieu|"
            r"o dau|khi nao|co.*khong|hoat dong|can luu y|can lam gi|can gi|"
            r"tac dung|ra sao|giup gi|loai nao|nhu nao|thi sao|xu ly|de lam gi|la.*gi|co nhung|thong tin|tinh nang)\b", text
        ))
        knowledge_question = knowledge_question and not _polite_control_request(text)
        rejection = scope_rejection_reason(input_text)
        if knowledge_question and rejection is None:
            return IntentDecision(route="handbook", intent="manual.search", arguments={"query": input_text})
        if knowledge_question and rejection and re.search(r"\b(vf\s*\d|xe|pin|sac|cua|ghe)\b", text):
            return IntentDecision(route="unsupported", intent="unsupported.request", response_text=rejection)
        if history and history[-1].get("route") == "action" and history[-1].get("intent") == "climate.set_temperature" and re.fullmatch(
            r"(?:tang|giam|ha|them|bot)(?: them| bot)? \d+(?:[.,]\d+)?(?: do)?[.!? ]*", text
        ):
            text = f"{text} do nhiet do"
        if history and history[-1].get("route") == "clarify" and re.fullmatch(r"\d+(?:[.,]\d+)?(?: do)?[.!? ]*", text):
            previous = _normalize(str(history[-1].get("query", "")))
            if re.search(r"nhiet do|dieu hoa|lanh|nong", previous):
                text = f"dat nhiet do {text} do"
        if self._cabin_zone(text) is not None and self._is_cabin_zone_reply(text) and history:
            previous = history[-1]
            previous_query = _normalize(str(previous.get("query", "")))
            previous_answer = _normalize(str(previous.get("answer", "")))
            if (
                previous.get("route") == "clarify"
                and re.search(r"\b(?:mo|dong|khoa|suoi|tat)\b", previous_query)
                and ("ben nao" in previous_answer or "vi tri nao" in previous_answer)
            ):
                text = f"{previous_query} {text}"
        if re.search(r"\b(phanh|danh lai|vo lang|tang toc|truyen dong|tat tui khi)\b", text):
            return IntentDecision(
                route="unsupported",
                intent="vehicle.prohibited",
                response_text="Mình không thể thực hiện yêu cầu can thiệp hệ thống an toàn hoặc vận hành xe.",
            )
        if re.search(r"\b(dung|khong)\s+(mo|dong|khoa|tang|giam|bat|tat|phat|ha)\b", text):
            return IntentDecision(
                route="clarify",
                intent="conversation.clarify",
                needs_clarification=True,
                clarification_question="Bạn muốn mình thực hiện thao tác nào?",
                response_text="Mình chưa chắc ý bạn. Bạn nói rõ thao tác giúp mình nhé.",
            )
        climate_request = re.search(
            r"nhiet do|dieu hoa|nong|lanh|mat hon|am hon|"
            r"\b(?:tang|giam|ha|them|bot)\b.{0,20}(?:do|°)",
            text,
        )
        if climate_request:
            if re.search(r"\b(bat|tat)\b", text) and not re.search(r"\d|\b(?:mot|hai|ba|bon|nam)\b", text):
                return IntentDecision(route="unsupported", intent="unsupported.request",
                                      response_text="Mình chưa bật hay tắt điều hoà bằng giọng nói được. Mình có thể chỉnh nhiệt độ.")
            amount = self._temperature_number(text)
            relative_cue = bool(
                re.search(r"\b(tang|giam|ha|them|bot)\b", text)
                or re.search(r"\b(hoi|qua|them)\s+(lanh|nong)\b|\b(mat|am)\s+hon\b", text)
                or (amount is None and re.search(r"\b(lanh|nong)\b", text))
            )
            # A number in the valid cabin range is overwhelmingly a target
            # ("tăng lên 25 độ"), while small numbers are deltas
            # ("tăng thêm 2 độ").
            relative = relative_cue and (amount is None or amount < 16)
            if amount is None and not relative:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn đặt nhiệt độ bao nhiêu?",
                    response_text="Bạn muốn đặt nhiệt độ bao nhiêu?",
                )
            if relative:
                if vehicle_state is None or vehicle_state.get("temperature_celsius") is None:
                    return IntentDecision(route="clarify", intent="conversation.clarify", needs_clarification=True,
                                          clarification_question="Bạn muốn đặt điều hoà bao nhiêu độ?")
                current = float(vehicle_state["temperature_celsius"])
                delta = amount if amount is not None else 2.0
                lower = bool(re.search(r"\b(giam|ha|bot)\b|\bmat hon\b", text)) or (
                    not re.search(r"\b(tang|them)\b|\bam hon\b", text) and "nong" in text
                )
                value = min(30.0, max(16.0, current + (-delta if lower else delta)))
            else:
                value = amount
            assert value is not None
            return IntentDecision(
                route="action",
                intent="climate.set_temperature",
                arguments={"value_celsius": value},
                response_text=f"Mình sẽ đặt điều hoà ở {value:g} độ nhé.",
            )
        if re.search(r"cua so|cua kinh|\bkinh\b", text):
            opening = bool(re.search(r"\b(mo|ha)\b", text))
            closing = bool(re.search(r"\b(dong|len)\b", text))
            if opening == closing:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn mở hay đóng cửa sổ?",
                    response_text="Bạn muốn mở hay đóng cửa sổ?",
                )
            zone = self._cabin_zone(text)
            if zone is None:
                action = "mở" if opening else "đóng"
                question = f"Bạn muốn {action} cửa sổ bên nào?"
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question=question,
                    response_text=question,
                )
            return IntentDecision(
                route="action",
                intent="window.set_position",
                arguments={"position_percent": int(percent.group(1)) if (percent := re.search(r"(\d+)\s*(?:%|phan tram)", text))
                           else 50 if "mot nua" in text else 100 if opening else 0, "zone": zone},
                response_text="Mình sẽ điều chỉnh cửa sổ nhé.",
            )
        door_request = re.search(
            r"\b(?:mo|dong)\s+(?:(?:tat ca|toan bo)\s+)?cua\b|"
            r"\b(?:mo\s+khoa|khoa)\s+(?:(?:tat ca|toan bo)\s+)?cua\b",
            text,
        )
        if door_request:
            if re.search(r"mo khoa|\bkhoa\s+(?:(?:tat ca|toan bo)\s+)?cua", text):
                zone = self._cabin_zone(text)
                if zone is None:
                    question = "Bạn muốn điều chỉnh khóa cửa ở vị trí nào?"
                    return IntentDecision(
                        route="clarify",
                        intent="conversation.clarify",
                        needs_clarification=True,
                        clarification_question=question,
                        response_text=question,
                    )
                return IntentDecision(
                    route="action",
                    intent="door.set_lock",
                    arguments={"locked": not bool(re.search(r"mo khoa", text)), "zone": zone},
                    response_text="Mình sẽ điều chỉnh khoá cửa nhé.",
                )
            opening = bool(re.search(r"\bmo\b", text))
            closing = bool(re.search(r"\bdong\b", text))
            if opening == closing:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn mở hay đóng cửa xe bên tài?",
                )
            zone = self._cabin_zone(text)
            if zone is None:
                action = "mở" if opening else "đóng"
                question = f"Bạn muốn {action} cửa bên nào?"
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question=question,
                    response_text=question,
                )
            return IntentDecision(
                route="action",
                intent="door.set_open",
                arguments={"open": opening, "zone": zone},
                response_text="Mình sẽ điều chỉnh cửa xe nhé.",
            )
        if re.search(r"suoi(?: (?:tat ca|toan bo))? ghe|ghe suoi|lam am ghe", text):
            match = re.search(r"(?:muc|cap)\s*(\d+)", text)
            level = 0 if re.search(r"\b(tat|dung)\b", text) else int(match.group(1)) if match else 1
            zone = self._cabin_zone(text)
            if zone is None:
                question = "Bạn muốn điều chỉnh sưởi ghế ở vị trí nào?"
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question=question,
                    response_text=question,
                )
            return IntentDecision(
                route="action",
                intent="seat.set_heat_level",
                arguments={"level": level, "zone": zone},
                response_text=f"Mình sẽ đặt sưởi ghế ở mức {level} nhé.",
            )
        if re.search(r"phat nhac|mo nhac", text):
            media_query = re.split(r"phát nhạc|mở nhạc|phat nhac|mo nhac", input_text, flags=re.IGNORECASE)[-1].strip(" .?!")
            return IntentDecision(route="action", intent="media.play", arguments={"media_query": media_query or None},
                                  response_text="Mình sẽ bật nhạc nhé.")
        if re.search(r"dung nhac|tat nhac", text):
            return IntentDecision(route="action", intent="media.pause", response_text="Mình sẽ dừng nhạc nhé.")
        if re.search(r"ban la ai|vivi la ai|gioi thieu.*ban", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text=(
                    "Mình là ViVi, tiếng nói của chiếc VF8 đang đồng hành cùng bạn."
                ),
            )
        if re.search(r"alo|nghe (thay|ro)|co nghe", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text="Mình nghe rõ. Bạn muốn mình hỗ trợ gì trên xe?",
            )
        if re.search(r"cam on", text):
            return IntentDecision(route="conversation", intent="conversation.respond", response_text="Không có gì nhé.")
        if re.search(r"xin chao|chao (?:vivi|ban)|^vivi[.!? ]*$", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text="Mình đây, bạn cần gì nhé?",
            )
        if re.search(r"ban (co the|lam duoc)|giup duoc gi|chuc nang cua ban", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text=(
                    "Mình có thể chỉnh nhiệt độ, cửa sổ, cửa xe, sưởi ghế, âm nhạc, "
                    "đọc trạng thái xe và tra cứu cẩm nang VF8."
                ),
            )
        if re.search(r"\b(bat|tat|mo|dong|chinh|kich hoat)\b.*(?:adas|cruise|tu lai|den pha|gat nuoc|guong|cop)", text):
            return IntentDecision(route="unsupported", intent="unsupported.request",
                                  response_text="Mình chưa điều khiển tính năng này bằng giọng nói được.")
        if re.search(r"\b(con|vay|the)\b.*(?:tinh nang|chuc nang).*(?:khac|gi)", text) and history and history[-1].get("route") == "handbook":
            return IntentDecision(route="handbook", intent="manual.search", arguments={"query": input_text})
        if rejection is None:
            return IntentDecision(route="handbook", intent="manual.search", arguments={"query": input_text})
        automotive = re.search(
            r"\b(vf8|xe|adas|cruise|sac|pin|bao duong|canh bao|thong so|"
            r"che do|lop|den|phanh|vo lang|dong co|cabin)\b",
            text,
        )
        question = "?" in input_text or re.search(
            r"\b(co khong|la gi|the nao|bao nhieu|tai sao|vi sao|cach|lam sao|"
            r"huong dan|cam nang|cho biet)\b",
            text,
        )
        if (automotive and question) or re.search(
            r"huong dan|cam nang|canh bao|sac|bao duong|thong so", text
        ):
            return IntentDecision(
                route="handbook",
                intent="manual.search",
                arguments={"query": input_text},
                response_text="Mình sẽ tra cứu cẩm nang VF8.",
            )
        return IntentDecision(
            route="clarify",
            intent="conversation.clarify",
            needs_clarification=True,
            clarification_question=(
                "Bạn muốn mình hỗ trợ việc gì trên xe?"
            ),
            response_text=(
                "Bạn muốn mình hỗ trợ việc gì trên xe?"
            ),
            confidence=0.4,
        )

    @staticmethod
    def _cabin_zone(text: str) -> str | None:
        if re.search(r"\b(tat ca|toan bo|ca bon|4)\b", text):
            return "all"
        if re.search(r"\b(sau|hang sau|phia sau).{0,12}\b(trai)\b", text):
            return "rear_left"
        if re.search(r"\b(sau|hang sau|phia sau).{0,12}\b(phai)\b", text):
            return "rear_right"
        if re.search(r"\b(ben phu|ghe phu|truoc phai)\b", text):
            return "front_passenger"
        if re.search(r"\b(ben tai|ben lai|tai xe|truoc trai)\b", text):
            return "driver"
        return None

    @staticmethod
    def _is_cabin_zone_reply(text: str) -> bool:
        """Accept a location-only clarification reply, not a new sentence mentioning one."""
        zone = (
            r"(?:ben tai|ben lai|tai xe|truoc trai|ben phu|ghe phu|truoc phai|"
            r"(?:sau|hang sau|phia sau)(?:\s+ben)?\s+(?:trai|phai)|"
            r"tat ca|toan bo|ca bon|4)"
        )
        return bool(
            re.fullmatch(
                rf"(?:(?:o|cua|ghe)\s+)?{zone}(?:\s+(?:nhe|a|giup minh|giup toi))?",
                text.strip(),
            )
        )

    @staticmethod
    def _temperature_number(text: str) -> float | None:
        numeric = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:do|°)", text)
        if not numeric:
            numeric = re.search(
                r"(?:nhiet do|dieu hoa).{0,20}?(\d+(?:[.,]\d+)?)\b",
                text,
            )
        if numeric:
            return float(numeric.group(1).replace(",", "."))
        words = {
            "khong": 0,
            "mot": 1,
            "hai": 2,
            "ba": 3,
            "bon": 4,
            "tu": 4,
            "nam": 5,
            "sau": 6,
            "bay": 7,
            "tam": 8,
            "chin": 9,
            "muoi": 10,
        }
        match = re.search(
            r"\b(khong|mot|hai|ba|bon|tu|nam|sau|bay|tam|chin|muoi)\s*(?:do|°)",
            text,
        )
        return float(words[match.group(1)]) if match else None
