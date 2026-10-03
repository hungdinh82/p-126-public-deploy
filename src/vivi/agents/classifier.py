from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from typing import Any

from src.vivi.agents.contracts import IntentDecision
from src.vivi.config import Settings
from src.vivi.structured_llm import StructuredChatClient, compact_history

CLASSIFIER_INSTRUCTION = """Bạn là bộ định tuyến cho trợ lý ô tô ViVi.
Nhận transcript tiếng Việt và trả JSON đúng schema. Chọn handbook/manual.search cho câu hỏi
về cách dùng, cảnh báo, thông số, sạc, bảo dưỡng hoặc tính năng VF8. Chọn action cho lệnh
điều khiển xe được hỗ trợ. Chọn clarify khi thiếu tham số quan trọng hoặc câu nói mơ hồ.
Phanh, lái, truyền động hay vô hiệu hóa an toàn phải là vehicle.prohibited/unsupported.
Không bao giờ nói một action đã hoàn tất; response_text chỉ được nói “mình sẽ” hoặc “cần
xác nhận”. Nhiệt độ hợp lệ 16-30°C, vị trí cửa sổ 0-100.
Với yêu cầu tăng/giảm nhiệt độ, dùng nhiệt độ hiện tại trong trạng thái xe để tính ra
value_celsius tuyệt đối. Không coi số độ tăng/giảm là nhiệt độ đích.
Tên argument bắt buộc theo intent: climate.set_temperature dùng value_celsius;
window.set_position dùng position_percent (0 là đóng, 100 là mở); door.set_open dùng open;
door.set_lock dùng locked; seat.set_heat_level dùng level từ 0 đến 3. Bốn intent cabin này
phải có zone: driver, front_passenger, rear_left, rear_right hoặc all;
manual.search dùng query; media.play có thể dùng media_query. Không tạo tên field khác.
Không được tự mặc định vị trí cửa xe. Với yêu cầu mở hoặc đóng cửa xe, nếu người dùng chưa
nói rõ cửa bên nào thì chọn conversation.clarify và hỏi “Bạn muốn mở/đóng cửa bên nào?”.
Hỗ trợ bốn vị trí cabin và “tất cả”. Không được tự mặc định vị trí; nếu thiếu zone thì clarify.
"""


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


class GoogleIntentClassifier(IntentClassifier):
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for intent classification")
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return self.classify_with_context(input_text, history, None)

    async def aclassify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return await self.aclassify_with_context(input_text, history, None)

    def classify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        from google.genai import types

        prompt = self._prompt(input_text, history, vehicle_state)
        config = types.GenerateContentConfig(
            system_instruction=CLASSIFIER_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=IntentDecision.model_json_schema(),
        )
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
                break
            except Exception as exc:
                transient = any(
                    marker in str(exc).upper()
                    for marker in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")
                )
                if not transient or attempt == 2:
                    raise
                time.sleep(0.75 * (2**attempt))
        if not response.text:
            raise RuntimeError("Gemini returned an empty intent decision")
        return IntentDecision.model_validate_json(response.text)

    async def aclassify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
    ) -> IntentDecision:
        from google.genai import types

        prompt = self._prompt(input_text, history, vehicle_state)
        config = types.GenerateContentConfig(
            system_instruction=CLASSIFIER_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=IntentDecision.model_json_schema(),
        )
        for attempt in range(3):
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config,
                )
                break
            except Exception as exc:
                transient = any(
                    marker in str(exc).upper()
                    for marker in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE")
                )
                if not transient or attempt == 2:
                    raise
                await asyncio.sleep(0.75 * (2**attempt))
        if not response.text:
            raise RuntimeError("Gemini returned an empty intent decision")
        return IntentDecision.model_validate_json(response.text)

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


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in value if unicodedata.category(char) != "Mn").replace("đ", "d")


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
        text = _normalize(input_text)
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
        if re.search(r"trang thai xe|pin con bao nhieu|quang duong con lai|xe con bao nhieu", text):
            return IntentDecision(
                route="action",
                intent="vehicle.get_status",
                response_text="Mình sẽ kiểm tra trạng thái xe.",
            )
        climate_request = re.search(
            r"nhiet do|dieu hoa|nong|lanh|mat hon|am hon|"
            r"\b(?:tang|giam|ha|them|bot)\b.{0,20}(?:do|°)",
            text,
        )
        if climate_request:
            if re.search(r"\b(bat|tat)\b", text) and not re.search(r"\d|\b(?:mot|hai|ba|bon|nam)\b", text):
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn đặt nhiệt độ bao nhiêu?",
                    response_text="Bạn muốn đặt nhiệt độ bao nhiêu?",
                )
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
                current = float((vehicle_state or {}).get("temperature_celsius", 23))
                delta = amount if amount is not None else 2.0
                lower = bool(re.search(r"\b(giam|ha|bot)\b|\bnong\b|\bmat hon\b", text))
                value = current + (-delta if lower else delta)
            else:
                value = amount
            assert value is not None
            return IntentDecision(
                route="action",
                intent="climate.set_temperature",
                arguments={"value_celsius": value},
                response_text=f"Mình sẽ đề xuất đặt nhiệt độ ở {value:g} độ.",
            )
        if re.search(r"cua so|cua kinh", text):
            opening = bool(re.search(r"\b(mo|ha)\b", text))
            closing = bool(re.search(r"\b(dong|len)\b", text))
            if not opening and not closing:
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
                arguments={"position_percent": 100 if opening else 0, "zone": zone},
                response_text="Mình đã tạo đề xuất điều chỉnh cửa sổ và đang chờ safety gateway.",
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
                    response_text="Mình sẽ đề xuất điều chỉnh khóa cửa.",
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
                response_text="Mình sẽ đề xuất điều chỉnh cửa xe.",
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
                response_text=f"Mình sẽ đề xuất đặt sưởi ghế mức {level}.",
            )
        if re.search(r"phat nhac|mo nhac", text):
            return IntentDecision(route="action", intent="media.play", response_text="Mình sẽ đề xuất phát nhạc.")
        if re.search(r"dung nhac|tat nhac", text):
            return IntentDecision(route="action", intent="media.pause", response_text="Mình sẽ đề xuất dừng nhạc.")
        if re.search(r"ban la ai|vivi la ai|gioi thieu.*ban", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text=(
                    "Mình là ViVi, trợ lý AI trên ô tô. Mình có thể điều khiển các tiện ích "
                    "cabin an toàn và trả lời câu hỏi từ cẩm nang VF8."
                ),
            )
        if re.search(r"alo|nghe (thay|ro)|co nghe", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text="Mình nghe rõ. Bạn muốn mình hỗ trợ gì trên xe?",
            )
        if re.search(r"xin chao|chao vivi|cam on|cam on ban", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text="Xin chào, mình là ViVi. Mình có thể hỗ trợ gì cho bạn?",
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
                "Mình đang ở chế độ offline. Bạn có thể hỏi về cẩm nang VF8 hoặc yêu cầu "
                "điều khiển tiện ích cabin."
            ),
            response_text=(
                "Mình đang ở chế độ offline. Bạn có thể hỏi về cẩm nang VF8 hoặc yêu cầu "
                "điều khiển tiện ích cabin."
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
