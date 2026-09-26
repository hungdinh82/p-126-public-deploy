from __future__ import annotations

import json
import re
import time
import unicodedata
from abc import ABC, abstractmethod

from src.agents.contracts import IntentDecision

CLASSIFIER_INSTRUCTION = """Bạn là bộ định tuyến cho trợ lý ô tô ViVi.
Nhận transcript tiếng Việt và trả JSON đúng schema. Chọn handbook/manual.search cho câu hỏi
về cách dùng, cảnh báo, thông số, sạc, bảo dưỡng hoặc tính năng VF8. Chọn action cho lệnh
điều khiển xe được hỗ trợ. Chọn clarify khi thiếu tham số quan trọng hoặc câu nói mơ hồ.
Phanh, lái, truyền động hay vô hiệu hóa an toàn phải là vehicle.prohibited/unsupported.
Không bao giờ nói một action đã hoàn tất; response_text chỉ được nói “mình sẽ” hoặc “cần
xác nhận”. Nhiệt độ hợp lệ 16-30°C, vị trí cửa sổ 0-100.
Tên argument bắt buộc theo intent: climate.set_temperature dùng value_celsius;
window.set_position dùng position_percent (0 là đóng, 100 là mở); door.set_open dùng open;
door.set_lock dùng locked; seat.set_heat_level dùng level từ 0 đến 3;
manual.search dùng query; media.play có thể dùng media_query. Không tạo tên field khác.
"""


class IntentClassifier(ABC):
    @abstractmethod
    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        raise NotImplementedError


class GoogleIntentClassifier(IntentClassifier):
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for intent classification")
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        from google.genai import types

        prompt = (
            f"Lịch sử gần đây: {json.dumps(history, ensure_ascii=False)}\n"
            f"Transcript hiện tại: {input_text}"
        )
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


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in value if unicodedata.category(char) != "Mn").replace("đ", "d")


class RulesIntentClassifier(IntentClassifier):
    """Deterministic offline classifier used by tests and degraded mode."""

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        text = _normalize(input_text)
        if re.search(r"\b(phanh|danh lai|vo lang|tang toc|truyen dong|tat tui khi)\b", text):
            return IntentDecision(
                route="unsupported",
                intent="vehicle.prohibited",
                response_text="Mình không thể thực hiện yêu cầu can thiệp hệ thống an toàn hoặc vận hành xe.",
            )
        if re.search(r"huong dan|cam nang|canh bao|sac|pin|bao duong|thong so|che do", text):
            return IntentDecision(route="handbook", intent="manual.search", arguments={"query": input_text})
        if re.search(r"nhiet do|dieu hoa|nong|lanh|mat hon|am hon", text):
            number = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:do|°)", text)
            if not number:
                return IntentDecision(
                    route="clarify",
                    intent="conversation.clarify",
                    needs_clarification=True,
                    clarification_question="Bạn muốn đặt nhiệt độ bao nhiêu?",
                    response_text="Bạn muốn đặt nhiệt độ bao nhiêu?",
                )
            value = float(number.group(1).replace(",", "."))
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
            return IntentDecision(
                route="action",
                intent="window.set_position",
                arguments={"position_percent": 100 if opening else 0},
                response_text="Mình đã tạo đề xuất điều chỉnh cửa sổ và đang chờ safety gateway.",
            )
        if re.search(r"\b(?:mo|dong|khoa)\s+(?:khoa\s+)?cua\b", text):
            if re.search(r"mo khoa|khoa cua", text):
                return IntentDecision(
                    route="action",
                    intent="door.set_lock",
                    arguments={"locked": not bool(re.search(r"mo khoa", text))},
                    response_text="Mình sẽ đề xuất điều chỉnh khóa cửa bên tài.",
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
            return IntentDecision(
                route="action",
                intent="door.set_open",
                arguments={"open": opening},
                response_text="Mình sẽ đề xuất điều chỉnh cửa xe bên tài.",
            )
        if re.search(r"suoi ghe|ghe suoi|lam am ghe", text):
            match = re.search(r"(?:muc|cap)\s*(\d+)", text)
            level = 0 if re.search(r"\b(tat|dung)\b", text) else int(match.group(1)) if match else 1
            return IntentDecision(
                route="action",
                intent="seat.set_heat_level",
                arguments={"level": level},
                response_text=f"Mình sẽ đề xuất đặt sưởi ghế mức {level}.",
            )
        if re.search(r"trang thai xe|pin con bao nhieu|quang duong con lai", text):
            return IntentDecision(
                route="action",
                intent="vehicle.get_status",
                response_text="Mình sẽ kiểm tra trạng thái xe.",
            )
        if re.search(r"phat nhac|mo nhac", text):
            return IntentDecision(route="action", intent="media.play", response_text="Mình sẽ đề xuất phát nhạc.")
        if re.search(r"dung nhac|tat nhac", text):
            return IntentDecision(route="action", intent="media.pause", response_text="Mình sẽ đề xuất dừng nhạc.")
        if re.search(r"xin chao|chao vivi|cam on", text):
            return IntentDecision(
                route="conversation",
                intent="conversation.respond",
                response_text="Xin chào, mình là ViVi. Mình có thể hỗ trợ gì cho bạn?",
            )
        return IntentDecision(
            route="clarify",
            intent="conversation.clarify",
            needs_clarification=True,
            clarification_question="Bạn có thể nói rõ yêu cầu liên quan đến xe không?",
            response_text="Bạn có thể nói rõ yêu cầu liên quan đến xe không?",
            confidence=0.4,
        )
