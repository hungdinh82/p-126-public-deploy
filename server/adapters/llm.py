from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from server.config import Settings
from server.schemas import ACTION_JSON_SCHEMA, ActionProposal, VehicleState


SYSTEM_PROMPT = """Bạn là ViVi, trợ lý AI đồng hành trong xe mô phỏng. Trả lời tự nhiên, ngắn gọn bằng tiếng Việt.
Chỉ chọn đúng một intent có trong enum của schema; tuyệt đối không phát minh intent mới. Khi tăng hoặc giảm nhiệt độ, luôn dùng climate.set_temperature và điền nhiệt độ mục tiêu tuyệt đối vào value_celsius; không dùng climate.increase_temperature hay climate.decrease_temperature. Với câu hỏi, chào hỏi hoặc trò chuyện không yêu cầu thao tác xe, chọn conversation.respond và viết câu trả lời vào spoken_response. Nếu được hỏi bạn là ai, hãy giới thiệu bạn là ViVi, trợ lý AI trên ô tô; không tự nhận là người hay đang kết nối xe thật.
Với lệnh xe, chọn intent tương ứng. Không khẳng định thao tác đã hoàn tất; hệ thống sẽ xác minh rồi mới thông báo. Không dùng conversation.respond để tuyên bố đã điều khiển xe.
Nếu lệnh xe mơ hồ, phủ định khó hiểu hoặc thiếu tham số quan trọng, chọn conversation.clarify.
Nhiệt độ hợp lệ 16-30°C. window.set_position dùng position_percent 0-100.
Điền đủ các trường JSON. arguments luôn gồm value_celsius, position_percent và query; đặt null cho trường không dùng."""


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in value if unicodedata.category(c) != "Mn").replace("đ", "d")


class LLMAdapter(ABC):
    name = "base"

    @abstractmethod
    async def propose(self, transcript: str, vehicle: VehicleState) -> ActionProposal:
        raise NotImplementedError

    async def stream_json(self, transcript: str, vehicle: VehicleState) -> AsyncIterator[str]:
        """Yield the structured proposal as it is generated.

        Providers without native streaming retain correct behavior through this
        fallback; they simply yield one complete JSON document.
        """
        proposal = await self.propose(transcript, vehicle)
        yield proposal.model_dump_json()


class RulesAdapter(LLMAdapter):
    name = "rules"

    async def propose(self, transcript: str, vehicle: VehicleState) -> ActionProposal:
        text = _normalize(transcript)
        if re.search(r"\b(dung|khong)\s+(mo|dong|khoa|tang|giam|bat|tat|phat|ha)\b", text):
            return ActionProposal(intent="conversation.clarify", needs_clarification=True, clarification_question="Bạn muốn mình thực hiện thao tác nào?", spoken_response="Mình chưa chắc ý bạn. Bạn nói rõ thao tác giúp mình nhé.")
        if re.search(r"huong dan|manual|cam nang|canh bao|ap suat", text):
            return ActionProposal(intent="manual.search", arguments={"query": transcript}, spoken_response="Mình sẽ mở phần hướng dẫn phù hợp.")
        if re.search(r"trang thai|pin|nhien lieu|bao nhieu|may do", text):
            return ActionProposal(intent="vehicle.get_status", spoken_response="Mình đang kiểm tra trạng thái xe.")
        if re.search(r"cua so|cua kinh", text):
            opening = bool(re.search(r"\b(mo|ha)\b", text))
            closing = bool(re.search(r"\b(dong|len)\b", text))
            if not opening and not closing:
                return ActionProposal(intent="conversation.clarify", needs_clarification=True, clarification_question="Bạn muốn mở hay đóng cửa sổ bên tài?", spoken_response="Bạn muốn mở hay đóng cửa sổ bên tài?")
            return ActionProposal(intent="window.set_position", arguments={"position_percent": 100 if opening else 0}, spoken_response="Mình sẽ điều chỉnh cửa sổ bên tài.")
        if re.search(r"\b(?:mo|dong|khoa)\s+(?:khoa\s+)?cua\b", text):
            if re.search(r"mo khoa|khoa cua", text):
                locked = not bool(re.search(r"mo khoa", text))
                return ActionProposal(intent="door.set_lock", arguments={"locked": locked}, spoken_response="Mình sẽ điều chỉnh khóa cửa bên tài.")
            opening = bool(re.search(r"\bmo\b", text))
            closing = bool(re.search(r"\bdong\b", text))
            if opening == closing:
                return ActionProposal(intent="conversation.clarify", needs_clarification=True, clarification_question="Bạn muốn mở hay đóng cửa xe bên tài?")
            return ActionProposal(intent="door.set_open", arguments={"open": opening}, spoken_response="Mình sẽ điều chỉnh cửa xe bên tài.")
        if re.search(r"suoi ghe|ghe suoi|lam am ghe", text):
            level_match = re.search(r"(?:muc|cap)\s*(\d+)", text)
            if re.search(r"\b(tat|dung)\b", text):
                level = 0
            elif level_match:
                level = int(level_match.group(1))
            elif re.search(r"\bbat\b", text):
                level = 1
            else:
                return ActionProposal(intent="conversation.clarify", needs_clarification=True, clarification_question="Bạn muốn sưởi ghế mức mấy (0 đến 3)?")
            return ActionProposal(intent="seat.set_heat_level", arguments={"level": level}, spoken_response=f"Mình sẽ đặt sưởi ghế mức {level}.")
        if re.search(r"nhac|bai hat|am thanh|play|pause", text):
            playing = not bool(re.search(r"dung|tat|pause", text))
            return ActionProposal(intent="media.play" if playing else "media.pause", spoken_response="Mình sẽ cập nhật trạng thái âm nhạc.")
        if re.search(r"lanh|am hon|nong|nhiet do|dieu hoa|mat hon", text):
            if re.search(r"\b(bat|tat)\b", text):
                return ActionProposal(intent="conversation.clarify", needs_clarification=True, clarification_question="Bạn muốn đặt nhiệt độ bao nhiêu?", spoken_response="Bạn muốn đặt nhiệt độ bao nhiêu?")
            number = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:do|°)", text)
            amount = float(number.group(1).replace(",", ".")) if number else 2
            lower = bool(re.search(r"\b(?:giam|ha)\b|\bnong\b|\bmat hon\b", text))
            relative = bool(re.search(r"\b(?:tang|giam|ha|them|bot)\b", text))
            value = amount if number and not relative else vehicle.temperature_celsius + (-amount if lower else amount)
            return ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": value}, spoken_response=f"Mình sẽ đặt nhiệt độ ở {value:g} độ.")
        return ActionProposal(intent="conversation.clarify", needs_clarification=True, clarification_question="Bạn muốn chỉnh nhiệt độ, cửa kính, cửa xe, sưởi ghế, âm nhạc hay xem trạng thái xe?")


def _prompt(transcript: str, vehicle: VehicleState) -> str:
    return f"{SYSTEM_PROMPT}\n\nTrạng thái xe: {vehicle.model_dump_json()}\nCâu nói: {transcript}"


class OpenAIAdapter(LLMAdapter):
    name = "openai"

    def __init__(self, config: Settings):
        if not config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY chưa được cấu hình")
        self.config = config
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=config.openai_api_key, timeout=config.llm_timeout_seconds)

    async def propose(self, transcript: str, vehicle: VehicleState) -> ActionProposal:
        response = await self.client.responses.create(
            model=self.config.openai_model,
            input=_prompt(transcript, vehicle),
            text={"format": {"type": "json_schema", "name": "vivi_action", "strict": True, "schema": ACTION_JSON_SCHEMA}},
        )
        return ActionProposal.model_validate_json(response.output_text)

    async def stream_json(self, transcript: str, vehicle: VehicleState) -> AsyncIterator[str]:
        async with self.client.responses.stream(
            model=self.config.openai_model,
            input=_prompt(transcript, vehicle),
            text={"format": {"type": "json_schema", "name": "vivi_action", "strict": True, "schema": ACTION_JSON_SCHEMA}},
        ) as stream:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield event.delta


class GoogleAdapter(LLMAdapter):
    name = "google"

    def __init__(self, config: Settings):
        if not config.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình")
        self.config = config
        from google import genai
        self.client = genai.Client(api_key=config.google_api_key)

    async def propose(self, transcript: str, vehicle: VehicleState) -> ActionProposal:
        from google.genai import types
        response = await self.client.aio.models.generate_content(
            model=self.config.google_model,
            contents=_prompt(transcript, vehicle),
            config=types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=ACTION_JSON_SCHEMA),
        )
        return ActionProposal.model_validate_json(response.text)

    async def stream_json(self, transcript: str, vehicle: VehicleState) -> AsyncIterator[str]:
        from google.genai import types
        stream = self.client.aio.models.generate_content_stream(
            model=self.config.google_model,
            contents=_prompt(transcript, vehicle),
            config=types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=ACTION_JSON_SCHEMA),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text


class LocalAPIAdapter(LLMAdapter):
    name = "local"

    def __init__(self, config: Settings):
        if not config.local_llm_model:
            raise RuntimeError("LOCAL_LLM_MODEL chưa được cấu hình")
        self.config = config

    async def propose(self, transcript: str, vehicle: VehicleState) -> ActionProposal:
        url = self.config.local_llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.local_llm_model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": _prompt(transcript, vehicle)}],
            "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {"name": "vivi_action", "strict": True, "schema": ACTION_JSON_SCHEMA}},
        }
        headers = {"Authorization": f"Bearer {self.config.local_llm_api_key}"}
        async with httpx.AsyncClient(timeout=self.config.llm_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content)
        return ActionProposal.model_validate_json(content)

    async def stream_json(self, transcript: str, vehicle: VehicleState) -> AsyncIterator[str]:
        url = self.config.local_llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.local_llm_model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": _prompt(transcript, vehicle)}],
            "temperature": 0,
            "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "vivi_action", "strict": True, "schema": ACTION_JSON_SCHEMA}},
        }
        headers = {"Authorization": f"Bearer {self.config.local_llm_api_key}"}
        async with httpx.AsyncClient(timeout=self.config.llm_timeout_seconds) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    payload = json.loads(data)
                    content = payload.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if isinstance(content, list):
                        content = "".join(part.get("text", "") for part in content)
                    if content:
                        yield content


def create_llm(config: Settings) -> LLMAdapter:
    providers = {"rules": RulesAdapter, "openai": OpenAIAdapter, "google": GoogleAdapter, "local": LocalAPIAdapter}
    cls = providers.get(config.llm_provider)
    if not cls:
        raise RuntimeError(f"LLM_PROVIDER không hợp lệ: {config.llm_provider}")
    return cls() if cls is RulesAdapter else cls(config)
