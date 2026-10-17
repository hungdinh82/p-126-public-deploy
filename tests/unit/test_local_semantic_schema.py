import json

import httpx
import pytest

from src.vivi.dialogue.contracts import SemanticTurn, SessionState
from src.vivi.dialogue.local_schema import compact_local_prompt, local_semantic_schema
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.model_gateway import ModelGateway
from src.vivi.structured_llm import StructuredChatClient, TruncatedCompletionError


def test_capability_branches_allow_sparse_arguments_but_not_cross_domain_slots():
    schema = local_semantic_schema()
    assert schema["anyOf"][0]["required"] == ["act", "actions"]
    assert schema["anyOf"][0]["properties"]["actions"]["minItems"] == 1
    variants = {item["properties"]["capability"]["const"]: item for item in schema["$defs"]["ActionDraft"]["anyOf"]}
    window = variants["window.set_position"]
    assert window["required"] == ["capability"]
    args = schema["$defs"][window["properties"]["arguments"]["$ref"].split("/")[-1]]
    assert not args.get("required")  # Missing targets must reach slot collection.
    assert args["additionalProperties"] is False
    assert set(args["properties"]) == {"position_percent", "zone", "zones", "target_side", "target_row"}
    assert set(
        schema["$defs"][variants["climate.set_temperature"]["properties"]["arguments"]["$ref"].split("/")[-1]][
            "properties"
        ]
    ) == {
        "value_celsius",
        "temperature_delta_celsius",
    }
    assert "Arguments" not in schema["$defs"]
    SemanticTurn.model_validate({"act": "request", "actions": [{"capability": "window.set_position"}]})


def test_compaction_preserves_pending_question_history_and_capability_meaning():
    source = {
        "utterance": "có",
        "tasks": {"pending_question": {"question_id": "q1"}},
        "recent_turns": [{"query": "mở kính", "answer": "Bên nào?"}],
        "capabilities": [
            {
                "name": "window.set_position",
                "description": "Open window",
                "read_only": False,
                "parameters": {"properties": {"zone": {}}},
            }
        ],
    }
    result = json.loads(compact_local_prompt(json.dumps(source)).split("\n", 1)[0])
    assert result["tasks"] == source["tasks"]
    assert result["recent_turns"] == source["recent_turns"]
    assert result["capabilities"] == [{"name": "window.set_position", "description": "Open window", "read_only": False}]
    assert compact_local_prompt("test prompt") == "test prompt"


@pytest.mark.asyncio
async def test_truncated_even_parseable_tool_call_never_repairs_or_executes():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "interpret_turn",
                                        "arguments": json.dumps(
                                            {
                                                "act": "request",
                                                "actions": [
                                                    {
                                                        "capability": "window.set_position",
                                                        "arguments": {"zone": "all", "position_percent": 100},
                                                    }
                                                ],
                                            }
                                        ),
                                    }
                                }
                            ]
                        },
                    }
                ]
            },
        )

    client = StructuredChatClient(
        base_url="http://test/v1", api_key="", model="qwen", timeout_seconds=1, transport=httpx.MockTransport(handler)
    )
    result, provider, attempts = await SemanticResolver({"local": ModelProvider("local", client, 1)}).resolve(
        "mở kính và bật nhạc", SessionState(), requested="local"
    )
    assert len(calls) == 1
    assert provider is None and not result.actions
    assert attempts[0]["reason"] == "TruncatedCompletionError"
    assert attempts[0]["completion"]["finish_reason"] == "length"


def test_truncated_json_response_is_rejected_before_parsing():
    client = StructuredChatClient(base_url="http://test/v1", api_key="", model="test", timeout_seconds=1)
    response = httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
    with pytest.raises(TruncatedCompletionError):
        client._response_content(response)


@pytest.mark.asyncio
async def test_configured_provider_budget_is_not_silently_capped_at_ten_seconds():
    gateway = ModelGateway({"local": ModelProvider("local", object(), 30)})

    async def operation(provider, attempts):
        assert 10 < attempts[-1]["budget_seconds"] <= 20
        return "ok"

    result = await gateway.run("semantic", operation, requested="local", budget_seconds=20)
    assert result.value == "ok"
