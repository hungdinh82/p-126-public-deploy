from __future__ import annotations

import asyncio
import copy
import json
import time
from contextvars import ContextVar
from typing import Any

import httpx


class TruncatedCompletionError(ValueError):
    """The provider exhausted its output/context budget; never execute partial data."""


def checked_choice(response: httpx.Response) -> dict:
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        raise TruncatedCompletionError("Model completion truncated by token/context limit")
    return choice


def strict_schema(schema: dict) -> dict:
    """Provider strict mode requires closed objects and every property required."""
    result = copy.deepcopy(schema)

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object" and "properties" in value:
                value["additionalProperties"] = False
                value["required"] = list(value["properties"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return result


def portable_schema(schema: dict) -> dict:
    """Gemini's compatibility endpoint rejects some Pydantic annotations.

    Keep types, enums and closed objects. Bounds remain enforced by the
    semantic validator after generation, including the single repair attempt.
    """
    result = copy.deepcopy(schema)

    def visit(value):
        if isinstance(value, dict):
            for key in ("default", "title", "minimum", "maximum", "minItems", "maxItems", "pattern"):
                value.pop(key, None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return result


class StructuredChatClient:
    """Small synchronous client for OpenAI-compatible JSON-schema completions."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_tokens: int = 512,
        transport: httpx.BaseTransport | None = None,
        extra_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        schema_in_prompt: bool = False,
        max_attempts: int = 3,
        schema_subset: bool = False,
        json_object_schema_names: frozenset[str] = frozenset(),
        execution_location: str | None = None,
        tools_as_json: bool = False,
        local_semantic_contract: str = "legacy",
    ) -> None:
        if not model:
            raise RuntimeError("A model name is required for structured generation")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.transport = transport
        self.extra_body = extra_body or {}
        self.extra_headers = extra_headers or {}
        # Some routed models accept response_format without enforcing the
        # schema, so the schema is also spelled out in the system prompt.
        self.schema_in_prompt = schema_in_prompt
        self.max_attempts = max(1, min(3, max_attempts))
        self.schema_subset = schema_subset
        self.json_object_schema_names = json_object_schema_names
        self.execution_location = execution_location
        self.tools_as_json = tools_as_json
        self.local_semantic_contract = local_semantic_contract
        self._completion_metadata = ContextVar(f"completion_metadata_{id(self)}", default=None)
        self._served_model: ContextVar[str | None] = ContextVar(f"served_model_{id(self)}", default=None)

    @property
    def served_model(self) -> str | None:
        """Model identifier confirmed by the response for the current task."""
        return self._served_model.get()

    @property
    def completion_metadata(self):
        return self._completion_metadata.get()

    def _capture_served_model(self, response: httpx.Response) -> None:
        try:
            value = response.json().get("model")
        except (ValueError, AttributeError, TypeError):
            value = None
        self._served_model.set(value if isinstance(value, str) and value.strip() else None)
        try:
            body = response.json()
            usage = body.get("usage") or {}
            self._completion_metadata.set(
                {
                    "finish_reason": body["choices"][0].get("finish_reason"),
                    **{
                        key: usage[key]
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if key in usage
                    },
                    "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    "timings": {
                        key: value
                        for key, value in (body.get("timings") or {}).items()
                        if key in {"cache_n", "prompt_n", "prompt_ms", "predicted_n", "predicted_ms"}
                        and isinstance(value, (int, float))
                    },
                }
            )
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            self._completion_metadata.set(None)

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        payload = self._request_payload(system, user, schema, schema_name)
        return self._response_content(self._complete(payload))

    def _complete(self, payload: dict) -> httpx.Response:
        self._served_model.set(None)
        self._completion_metadata.set(None)
        headers = self._headers()
        for attempt in range(self.max_attempts):
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = client.post(self.url, json=payload, headers=headers)
                    response.raise_for_status()
                self._capture_served_model(response)
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if not self._retryable(exc) or attempt == self.max_attempts - 1:
                    raise
                time.sleep(0.25 * (2**attempt))
        raise RuntimeError("structured generation failed")

    async def agenerate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        payload = self._request_payload(system, user, schema, schema_name)
        return self._response_content(await self._acomplete(payload))

    async def _acomplete(self, payload: dict) -> httpx.Response:
        self._served_model.set(None)
        self._completion_metadata.set(None)
        headers = self._headers()
        for attempt in range(self.max_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = await client.post(self.url, json=payload, headers=headers)
                    response.raise_for_status()
                self._capture_served_model(response)
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if not self._retryable(exc) or attempt == self.max_attempts - 1:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError("structured generation failed")

    def _tool_payload(self, system: str, user: str, tools: list[dict]) -> dict:
        if self.tools_as_json:
            if len(tools) != 1:
                raise ValueError("JSON tool transport requires exactly one interpretation tool")
            function = tools[0]["function"]
            # llama.cpp constrains generation with this schema without injecting
            # it into the chat template. Preserve sparse optional slots: provider
            # strict_schema would force every irrelevant field into the output.
            return {
                **self.extra_body,
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": function["name"], "strict": False, "schema": function["parameters"]},
                },
                "temperature": 0,
                "max_tokens": self.max_tokens,
            }
        return {
            **self.extra_body,
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": tools,
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }

    @staticmethod
    def _tool_result(response: httpx.Response) -> tuple[str, dict]:
        message = checked_choice(response)["message"]
        calls = message.get("tool_calls") or []
        if len(calls) != 1:
            raise ValueError("Planner must return exactly one tool call")
        call = calls[0]["function"]
        arguments = json.loads(call["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        return call["name"], arguments

    def generate_tool(self, *, system: str, user: str, tools: list[dict]) -> tuple[str, dict]:
        response = self._complete(self._tool_payload(system, user, tools))
        return self._interpretation_result(response, tools)

    async def agenerate_tool(self, *, system: str, user: str, tools: list[dict]) -> tuple[str, dict]:
        response = await self._acomplete(self._tool_payload(system, user, tools))
        return self._interpretation_result(response, tools)

    async def agenerate_proposals(
        self, *, system: str, user: str, tools: list[dict], examples=None
    ) -> list[tuple[str, dict]]:
        """Same typed proposal tools over JSON grammar or native function calls."""
        if self.tools_as_json:
            schema = self.proposal_schema(tools)
            wrapper = [{"type": "function", "function": {"name": "local_proposals", "parameters": schema}}]
            payload = self._tool_payload(system, user, wrapper)
            if examples:
                payload["messages"][1:1] = examples
            response = await self._acomplete(payload)
            value = json.loads(self._response_content(response))
            if (
                not isinstance(value, dict)
                or set(value) != {"calls"}
                or not isinstance(value["calls"], list)
                or not 1 <= len(value["calls"]) <= 5
            ):
                raise ValueError("Invalid proposal envelope")
            if any(
                not isinstance(call, dict)
                or set(call) != {"name", "arguments"}
                or not isinstance(call["name"], str)
                or not isinstance(call["arguments"], dict)
                for call in value["calls"]
            ):
                raise ValueError("Each proposal needs a name and argument object")
            # Preserve raw proposals for audit; the adapter checks every tool's
            # schema before translating anything into a domain event.
            return [(call["name"], call["arguments"]) for call in value["calls"]]
        wire_tools = [{"type": t["type"], "function": t["function"]} for t in tools]
        payload = self._tool_payload(system, user, wire_tools)
        if examples:
            payload["messages"][1:1] = examples
        payload["parallel_tool_calls"] = True
        message = checked_choice(await self._acomplete(payload))["message"]
        calls = message.get("tool_calls") or []
        if not 1 <= len(calls) <= 5:
            raise ValueError("Expected one to five proposal calls")
        result = []
        for call in calls:
            function = call["function"]
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("Proposal arguments must be an object")
            result.append((function["name"], arguments))
        return result

    @staticmethod
    def proposal_schema(tools: list[dict]) -> dict:
        return {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "const": tool["function"]["name"]},
                                    "arguments": tool["function"]["parameters"],
                                },
                                "required": ["name", "arguments"],
                                "additionalProperties": False,
                            }
                            for tool in tools
                        ]
                    },
                }
            },
            "required": ["calls"],
            "additionalProperties": False,
        }

    def _interpretation_result(self, response: httpx.Response, tools: list[dict]) -> tuple[str, dict]:
        if not self.tools_as_json:
            return self._tool_result(response)
        arguments = json.loads(self._response_content(response))
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        return tools[0]["function"]["name"], arguments

    def _request_payload(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        if self.schema_subset:
            schema = portable_schema(schema)
        if self.schema_in_prompt:
            system = (
                f"{system}\nChỉ trả về một object JSON hợp lệ theo JSON Schema sau, "
                f"không thêm chữ nào khác:\n{json.dumps(schema, ensure_ascii=False)}"
            )
        # Gemini accepts small structured answers but rejects the complete
        # semantic DTO (HTTP 400). JSON mode still receives its schema in the
        # prompt; SemanticTurn validation and repair remain authoritative.
        response_format = (
            {"type": "json_object"}
            if schema_name in self.json_object_schema_names
            else {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": strict_schema(schema)},
            }
        )
        return {
            **self.extra_body,
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": response_format,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return {**headers, **self.extra_headers}

    def _response_content(self, response: httpx.Response) -> str:
        content = checked_choice(response)["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content)
        return self._normalize_json(str(content))

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        return not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {
            429,
            500,
            502,
            503,
            504,
        }

    @staticmethod
    def _normalize_json(content: str) -> str:
        value = content.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
        # Validate here so failures are attributed to the inference boundary.
        json.loads(value)
        return value


def google_client(config: Any) -> StructuredChatClient:
    """Gemini's OpenAI-compatible endpoint, with independent credentials/budget."""
    return StructuredChatClient(
        base_url=config.google_base_url,
        api_key=config.google_api_key,
        model=config.google_model,
        timeout_seconds=config.llm_timeout_seconds,
        max_tokens=config.google_max_tokens,
        extra_body={"reasoning_effort": config.google_reasoning_effort},
        schema_in_prompt=True,
        max_attempts=1,
        schema_subset=True,
        json_object_schema_names=frozenset({"vivi_semantic_turn"}),
    )


def openrouter_client(config: Any) -> StructuredChatClient:
    """OpenAI-compatible client for OpenRouter with ViVi's latency defaults."""

    return StructuredChatClient(
        base_url=config.openrouter_base_url,
        api_key=config.openrouter_api_key,
        model=config.openrouter_model,
        timeout_seconds=config.llm_timeout_seconds,
        max_tokens=config.openrouter_max_tokens,
        extra_body={"reasoning": {"enabled": config.openrouter_reasoning}},
        extra_headers={"X-Title": "ViVi Cabin Copilot"},
        schema_in_prompt=True,
    )


def compact_history(history: list[dict], *, limit: int = 3) -> list[dict[str, str]]:
    """Keep only conversational fields that can influence the next model call."""

    compact: list[dict[str, str]] = []
    for item in history[-limit:]:
        query = str(item.get("query") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if query or answer:
            compact.append({"query": query[:500], "answer": answer[:700]})
    return compact
