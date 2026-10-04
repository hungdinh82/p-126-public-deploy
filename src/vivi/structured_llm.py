from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx


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

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        payload = self._request_payload(system, user, schema, schema_name)
        headers = self._headers()
        for attempt in range(3):
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = client.post(self.url, json=payload, headers=headers)
                    response.raise_for_status()
                return self._response_content(response)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if not self._retryable(exc) or attempt == 2:
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
        headers = self._headers()
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = await client.post(self.url, json=payload, headers=headers)
                    response.raise_for_status()
                return self._response_content(response)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if not self._retryable(exc) or attempt == 2:
                    raise
                await asyncio.sleep(0.25 * (2**attempt))
        raise RuntimeError("structured generation failed")

    def _request_payload(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        if self.schema_in_prompt:
            system = (
                f"{system}\nChỉ trả về một object JSON hợp lệ theo JSON Schema sau, "
                f"không thêm chữ nào khác:\n{json.dumps(schema, ensure_ascii=False)}"
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
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return {**headers, **self.extra_headers}

    def _response_content(self, response: httpx.Response) -> str:
        content = response.json()["choices"][0]["message"]["content"]
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
