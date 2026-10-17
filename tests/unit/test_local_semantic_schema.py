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
    assert result["capabilities"] == [
        {
            "name": "window.set_position",
            "description": "Open window",
            "slots": ["position_percent", "target_row", "target_side", "zone", "zones"],
        }
    ]
    assert compact_local_prompt(json.dumps(source)).endswith('Câu người dùng hiện tại: "có"')
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


def test_session_schema_cannot_invent_questions_tasks_or_start_consent():
    schema = local_semantic_schema({"tasks": {}, "capabilities": []})
    acts = {act for branch in schema["anyOf"] for act in branch["properties"]["act"]["enum"]}
    assert not acts.intersection({"answer", "confirm", "deny", "modify", "resume", "suspend"})
    capabilities = [v["properties"]["capability"]["const"] for v in schema["$defs"]["ActionDraft"]["anyOf"]]
    assert "navigation.start" not in capabilities
    assert "media.play" in capabilities

    schema = local_semantic_schema(
        {
            "tasks": {
                "pending_question": {"id": "current-question", "purpose": "approve_execution"},
                "active": {"handle": "active"},
                "suspended": [{"handle": "suspended:0"}],
            },
            "capabilities": [{"name": "navigation.start"}, {"name": "media.play"}],
        }
    )
    assert schema["$defs"]["QuestionReply"]["properties"]["question_id"]["const"] == "current-question"
    assert {v["properties"]["capability"]["const"] for v in schema["$defs"]["ActionDraft"]["anyOf"]} == {
        "navigation.start",
        "media.play",
    }
    modify = next(b for b in schema["anyOf"] if "modify" in b["properties"]["act"]["enum"])
    assert modify["properties"]["task_ref"]["enum"] == ["active", "suspended:0"]


def test_navigation_projection_preserves_references_and_never_changes_checkpoint():
    from copy import deepcopy

    from src.vivi.dialogue.local_schema import local_json_prompt

    destination = {"name": "Bảo tàng", "area": {"text": "Huế", "source": "user", "constraint": "required"}}
    source = {
        "utterance": "đi chỗ đó",
        "tasks": {
            "pending_question": {"id": "q1", "choices": [{"handle": "choice-1", "label": "Bảo tàng"}]},
            "active": {"handle": "active", "editable_steps": [1], "steps": [{"arguments": {"query": "Bảo tàng"}}]},
            "navigation": {
                "draft": {
                    "id": "plan-1",
                    "revision": 5,
                    "destination": destination,
                    "selected": {"handle": "place-1", "name": "Bảo tàng", "provider_evidence": "x" * 20000},
                    "clarification": {"choices": [{"handle": "choice-1", "query": "Bảo tàng, Huế"}]},
                    "route": {"coordinates": [[1, 2]] * 20000},
                    "offer": {"grant": {"private_binding": "secret"}, "status": "pending", "question": "Đi nhé?"},
                }
            },
        },
        "capabilities": [{"name": "media.play", "description": "Phát nhạc"}],
    }
    before = deepcopy(source)
    prompt = compact_local_prompt(json.dumps(source))
    context = json.loads(prompt.split("\n", 1)[0])
    assert context["tasks"]["pending_question"] == source["tasks"]["pending_question"]
    assert context["tasks"]["active"] == source["tasks"]["active"]
    nav = context["tasks"]["navigation"]["draft"]
    assert nav["id"] == "plan-1" and nav["revision"] == 5 and nav["destination"] == destination
    assert nav["clarification"] == source["tasks"]["navigation"]["draft"]["clarification"]
    assert nav["selected"] == {"handle": "place-1", "name": "Bảo tàng"}
    assert "route" not in nav and "grant" not in nav["offer"]
    assert source == before
    system, user = local_json_prompt("instructions", prompt)
    assert "media.play" in system and "capabilities" not in json.loads(user.split("\n", 1)[0])
    assert user.endswith('Câu người dùng hiện tại: "đi chỗ đó"')


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_json_tool_transport_keeps_schema_out_of_prompt_and_rejects_truncation(finish):
    calls = []
    frame = {"act": "request", "actions": [{"capability": "media.play"}]}

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert "tools" not in payload and "tool_choice" not in payload
        assert "$defs" not in payload["messages"][0]["content"]
        contract = payload["response_format"]["json_schema"]
        assert contract["strict"] is False
        assert contract["schema"]["anyOf"][0]["required"] == ["act", "actions"]
        return httpx.Response(
            200,
            json={
                "model": "qwen",
                "choices": [{"finish_reason": finish, "message": {"content": json.dumps(frame)}}],
                "usage": {
                    "prompt_tokens": 2000,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 1900},
                },
                "timings": {"prompt_ms": 100, "predicted_ms": 200, "unexpected": "private"},
            },
        )

    client = StructuredChatClient(
        base_url="http://test/v1",
        api_key="",
        model="qwen",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
        tools_as_json=True,
    )
    value, provider, attempts = await SemanticResolver({"local": ModelProvider("local", client, 1)}).resolve(
        "bật nhạc",
        SessionState(),
        requested="local",
    )
    assert len(calls) == 1
    assert attempts[0]["completion"]["cached_tokens"] == 1900
    assert attempts[0]["completion"]["timings"] == {"prompt_ms": 100, "predicted_ms": 200}
    if finish == "length":
        assert provider is None and not value.actions
        assert attempts[0]["reason"] == "TruncatedCompletionError"
    else:
        assert provider == "local" and value.actions[0].capability == "media.play"
        assert attempts[0]["semantic_validation"] == "accepted"


@pytest.mark.asyncio
async def test_rejected_semantics_preserve_the_original_proposal_in_diagnostics():
    class Client:
        async def agenerate_tool(self, **kwargs):
            return "interpret_turn", {"act": "request", "actions": [{"capability": "navigation.start"}]}

    value, provider, attempts = await SemanticResolver({"local": ModelProvider("local", Client(), 1)}).resolve(
        "bật nhạc",
        SessionState(),
        requested="local",
    )
    assert provider == "local" and value.act == "clarify" and not value.actions
    assert attempts[0]["proposed_capabilities"] == ["navigation.start"]
    assert attempts[0]["semantic_validation"] == "rejected"
