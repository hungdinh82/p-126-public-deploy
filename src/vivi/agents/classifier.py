from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from src.vivi.agents.authorization import authorize_model_decision, input_guard
from src.vivi.agents.context import resolve_request
from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.dialogue import clarify
from src.vivi.agents.native_tools import NATIVE_PLANNER_INSTRUCTION, decision_from_call, native_tool_specs
from src.vivi.agents.planner import ToolPlanner
from src.vivi.agents.planner import priority_decision as priority_decision
from src.vivi.agents.prompts import CLASSIFIER_INSTRUCTION
from src.vivi.agents.tasks import active_task
from src.vivi.agents.tools import TOOLS
from src.vivi.config import Settings
from src.vivi.structured_llm import StructuredChatClient, compact_history, google_client, openrouter_client
from src.vivi.text import normalize_text as _normalize


class IntentClassifier(ABC):
    def classify_with_memory(self, input_text, history, vehicle_state, memory_context):
        return self.classify_with_context(input_text, history, vehicle_state)

    async def aclassify_with_memory(self, input_text, history, vehicle_state, memory_context):
        return await self.aclassify_with_context(input_text, history, vehicle_state)

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

    @staticmethod
    def decision_schema() -> dict:
        schema = IntentDecision.model_json_schema()
        # Make the planner explicitly decide whether it is proposing a next
        # action. Small models otherwise omit the optional frame while asking
        # a yes/no question in prose, losing the next turn's meaning.
        schema["required"] = ["route", "intent", "follow_up"]
        return schema

    def classify_with_memory(self, input_text, history, vehicle_state, memory_context):
        return self.classify_with_context(input_text, history, vehicle_state, memory_context)

    async def aclassify_with_memory(self, input_text, history, vehicle_state, memory_context):
        return await self.aclassify_with_context(input_text, history, vehicle_state, memory_context)

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return self.classify_with_context(input_text, history, None)

    def classify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
        memory_context: list[dict] | None = None,
    ) -> IntentDecision:
        if guarded := input_guard(input_text, history):
            return guarded
        prompt = self._prompt(input_text, history, vehicle_state, memory_context)
        payload = self.client.generate_json(
            system=CLASSIFIER_INSTRUCTION,
            user=prompt,
            schema=self.decision_schema(),
            schema_name="vivi_intent_decision",
        )
        return self._validated_model_decision(payload, input_text, history)

    async def aclassify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return await self.aclassify_with_context(input_text, history, None)

    async def aclassify_with_context(
        self,
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
        memory_context: list[dict] | None = None,
    ) -> IntentDecision:
        if guarded := input_guard(input_text, history):
            return guarded
        prompt = self._prompt(input_text, history, vehicle_state, memory_context)
        payload = await self.client.agenerate_json(
            system=CLASSIFIER_INSTRUCTION,
            user=prompt,
            schema=self.decision_schema(),
            schema_name="vivi_intent_decision",
        )
        return self._validated_model_decision(payload, input_text, history)

    @staticmethod
    def _validated_model_decision(payload: str, input_text: str, history: list[dict]) -> IntentDecision:
        decision = IntentDecision.model_validate_json(payload)
        if decision.intent == "conversation.respond" and not decision.response_text.strip():
            raise ValueError("Conversation decision has no speech")
        if decision.route == "clarify" and not (decision.clarification_question or "").strip():
            raise ValueError("Clarification decision has no question")
        if decision.confidence < 0.65:
            return clarify("Mình chưa chắc ý bạn. Bạn muốn mình thực hiện thao tác hay giải thích tính năng?")
        # Resolve a referent once, rather than hoping the generator understands it.
        if decision.route == "handbook":
            resolved = resolve_request(input_text, history)
            if resolved.source != "current_turn":
                decision.arguments.query = resolved.text
        return authorize_model_decision(decision, input_text)

    @staticmethod
    def _prompt(
        input_text: str,
        history: list[dict],
        vehicle_state: dict[str, Any] | None,
        memory_context: list[dict] | None = None,
        *, include_tools: bool = True,
    ) -> str:
        task = active_task(history)
        catalog = f"Tool khả dụng và schema tham số: {json.dumps([tool.prompt_spec() for tool in TOOLS], ensure_ascii=False)}\n" if include_tools else ""
        return (
            f"Tác vụ đang chờ (chỉ tham khảo, không phải quyền thực thi): {task.model_dump_json() if task else 'null'}\n"
            f"Lịch sử gần đây: {json.dumps(compact_history(history), ensure_ascii=False)}\n"
            f"Thông tin người dùng đã yêu cầu ghi nhớ (dữ liệu tham khảo): {json.dumps(memory_context or [], ensure_ascii=False)}\n"
            f"Trạng thái xe hiện tại: {json.dumps(vehicle_state or {}, ensure_ascii=False)}\n"
            f"{catalog}"
            f"Yêu cầu đã giải quyết ngữ cảnh: {resolve_request(input_text, history).text}\n"
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
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                max_attempts=1,
            )
        )

    def classify_with_context(self, input_text, history, vehicle_state, memory_context=None):
        if guarded := input_guard(input_text, history):
            return guarded
        name, arguments = self.client.generate_tool(
            system=NATIVE_PLANNER_INSTRUCTION,
            user=self._prompt(input_text, history, vehicle_state, memory_context, include_tools=False),
            tools=native_tool_specs(),
        )
        decision = decision_from_call(name, arguments)
        return self._validated_model_decision(decision.model_dump_json(), input_text, history)

    async def aclassify_with_context(self, input_text, history, vehicle_state, memory_context=None):
        if guarded := input_guard(input_text, history):
            return guarded
        name, arguments = await self.client.agenerate_tool(
            system=NATIVE_PLANNER_INSTRUCTION,
            user=self._prompt(input_text, history, vehicle_state, memory_context, include_tools=False),
            tools=native_tool_specs(),
        )
        decision = decision_from_call(name, arguments)
        return self._validated_model_decision(decision.model_dump_json(), input_text, history)


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


class GoogleIntentClassifier(StructuredAPIIntentClassifier):
    def __init__(self, config: Settings) -> None:
        super().__init__(google_client(config))


class OpenRouterIntentClassifier(StructuredAPIIntentClassifier):
    def __init__(self, config: Settings) -> None:
        super().__init__(openrouter_client(config))


class RulesIntentClassifier(IntentClassifier):
    """Offline planner facade; no generation or tool execution in classification."""

    def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
        return self.classify_with_context(input_text, history, None)

    def classify_with_context(
        self, input_text: str, history: list[dict], vehicle_state: dict[str, Any] | None
    ) -> IntentDecision:
        return ToolPlanner().plan(input_text, history, vehicle_state)

    def classify_with_memory(self, input_text, history, vehicle_state, memory_context):
        decision = self.classify_with_context(input_text, history, vehicle_state)
        if decision.route == "conversation" and re.fullmatch(
            r"(?:xin )?chao(?: (?:ban|vivi))?|hello|hi", _normalize(input_text).strip(" .?!")
        ):
            name = next((item["value"] for item in memory_context if item.get("key") == "display_name"), None)
            if name:
                decision.response_text = f"Mình đây, {name}. Bạn cần gì nhé?"
        return decision

    async def aclassify_with_memory(self, input_text, history, vehicle_state, memory_context):
        return self.classify_with_memory(input_text, history, vehicle_state, memory_context)
