from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from server.config import Settings
from server.schemas import ACTION_JSON_SCHEMA, ActionProposal, VehicleState
from src.agents.classifier import RulesIntentClassifier

SYSTEM_PROMPT = """Bạn là ViVi, trợ lý AI đồng hành trong xe mô phỏng. Trả lời tự nhiên, ngắn gọn bằng tiếng Việt.
Chỉ chọn đúng một intent có trong enum của schema; tuyệt đối không phát minh intent mới. Khi tăng hoặc giảm nhiệt độ, luôn dùng climate.set_temperature và điền nhiệt độ mục tiêu tuyệt đối vào value_celsius; không dùng climate.increase_temperature hay climate.decrease_temperature. Với câu hỏi, chào hỏi hoặc trò chuyện không yêu cầu thao tác xe, chọn conversation.respond và viết câu trả lời vào spoken_response. Nếu được hỏi bạn là ai, hãy giới thiệu bạn là ViVi, trợ lý AI trên ô tô; không tự nhận là người hay đang kết nối xe thật.
Với lệnh xe, chọn intent tương ứng. Không khẳng định thao tác đã hoàn tất; hệ thống sẽ xác minh rồi mới thông báo. Không dùng conversation.respond để tuyên bố đã điều khiển xe.
Nếu lệnh xe mơ hồ, phủ định khó hiểu hoặc thiếu tham số quan trọng, chọn conversation.clarify.
Nhiệt độ hợp lệ 16-30°C. window.set_position dùng position_percent 0-100.
Điền đủ các trường JSON. arguments luôn gồm value_celsius, position_percent và query; đặt null cho trường không dùng."""


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
        decision = RulesIntentClassifier().classify_with_context(
            transcript,
            [],
            vehicle.model_dump(mode="json"),
        )
        response = decision.response_text or decision.clarification_question or ""
        return ActionProposal(
            intent=decision.intent,
            arguments=decision.arguments.model_dump(exclude_none=True),
            needs_clarification=decision.needs_clarification,
            clarification_question=decision.clarification_question,
            spoken_response=response,
            confidence=decision.confidence,
        )


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
