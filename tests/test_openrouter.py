from __future__ import annotations

import json

import httpx

from src.vivi.agents.contracts import IntentDecision
from src.vivi.config import Settings
from src.vivi.providers import configured_llm_providers
from src.vivi.structured_llm import openrouter_client


def _settings(**overrides) -> Settings:
    return Settings(openrouter_api_key="or-test", **overrides)


def test_openrouter_is_configured_only_with_key():
    assert "openrouter" in configured_llm_providers(_settings())
    assert "openrouter" not in configured_llm_providers(Settings(openrouter_api_key=""))


def test_openrouter_request_disables_reasoning_and_spells_out_schema():
    captured = {}
    decision = {
        "route": "conversation",
        "intent": "conversation.respond",
        "arguments": {},
        "response_text": "Chào bạn",
        "confidence": 0.9,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})

    client = openrouter_client(_settings())
    client.transport = httpx.MockTransport(handler)
    client.generate_json(
        system="Bạn là ViVi.",
        user="Xin chào",
        schema=IntentDecision.model_json_schema(),
        schema_name="vivi_intent_decision",
    )

    body = captured["body"]
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer or-test"
    assert body["model"] == "qwen/qwen3.7-flash"
    assert body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == 1536
    assert '"intent"' in body["messages"][0]["content"]
