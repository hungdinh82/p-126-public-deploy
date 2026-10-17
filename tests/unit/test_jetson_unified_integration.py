"""Jetson routing preserves main's single workflow and vehicle effect ledger."""

import json

import httpx
import pytest

from jetson_ai.app import ServiceConfig, create_app
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.structured_llm import StructuredChatClient
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.support.dialogue_v2 import ScriptedModel
from tests.support.long_term_memory import services as _services

services = _services


@pytest.mark.asyncio
async def test_unified_semantic_tool_contract_through_authenticated_jetson_gateway():
    def engine(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        payload = json.loads(request.content)
        assert payload["model"] == "vivi-qwen3-1.7b"
        assert payload["tool_choice"] == "required"
        assert payload["tools"][0]["function"]["name"] == "interpret_turn"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "proposal",
                                    "type": "function",
                                    "function": {
                                        "name": "interpret_turn",
                                        "arguments": json.dumps(
                                            {
                                                "act": "request",
                                                "actions": [
                                                    {
                                                        "capability": "climate.set_temperature",
                                                        "arguments": {"value_celsius": 24},
                                                    }
                                                ],
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    gateway = create_app(ServiceConfig(token="s" * 32), transport=httpx.MockTransport(engine))
    client = StructuredChatClient(
        base_url="http://jetson.test/v1",
        api_key="s" * 32,
        model="vivi-qwen3-1.7b",
        timeout_seconds=1,
        transport=httpx.ASGITransport(app=gateway),
    )
    resolver = SemanticResolver({"local": ModelProvider("local", client, 1)})
    proposal, served, _ = await resolver.resolve("đặt 24 độ", SessionState(), requested="local")
    assert served == "local"
    assert proposal.actions[0].capability == "climate.set_temperature"
    assert proposal.actions[0].arguments.value_celsius == 24


@pytest.mark.asyncio
async def test_jetson_failure_uses_cloud_in_same_workflow_and_executes_once(services, tmp_path):
    local = ScriptedModel([ConnectionError("Jetson disappeared after readiness probe")])
    cloud = ScriptedModel(
        [{"act": "request", "actions": [{"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}}]}]
    )
    ledger = RuntimeStore(tmp_path / "effects.sqlite3")
    gateway = VehicleActionGateway(services.action_gateway.vehicle, DurableConfirmations(ledger), ledger=ledger)
    services.action_gateway = gateway
    resolver = SemanticResolver(
        {"local": ModelProvider("local", local, 0.2), "google": ModelProvider("google", cloud, 0.2)},
        fallback_chains={"local": ["local", "google"]},
    )
    graph = build_assistant_graph(
        services, ThreadedSqliteSaver(tmp_path / "conversation.sqlite3"), ledger, resolver=resolver
    )
    event = {"session_id": "driver", "turn_id": "one", "input_text": "đặt nhiệt độ 24 độ", "provider": "local"}
    try:
        result = await graph.ainvoke(event)
        assert result["diagnostics"]["effective_provider"] == "google"
        assert [a["provider"] for a in result["diagnostics"]["provider_attempts"]] == ["local", "google"]
        assert gateway.vehicle.state_for("driver").temperature_celsius == 24
        assert gateway.vehicle.state_for("driver").state_version == 1
        await graph.ainvoke(event)
        assert gateway.vehicle.state_for("driver").state_version == 1
        assert len(cloud.prompts) == 1
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_offline_semantic_chain_never_calls_configured_cloud():
    cloud = ScriptedModel([AssertionError("offline_only must not call cloud")])
    local = ScriptedModel([ConnectionError("Jetson offline")])
    resolver = SemanticResolver(
        {"local": ModelProvider("local", local, 0.2), "google": ModelProvider("google", cloud, 0.2)},
        fallback_chains={"local": ["local"]},
    )
    _, served, attempts = await resolver.resolve("xin chào", SessionState(), requested="local")
    assert served is None and not cloud.prompts
    assert [a["provider"] for a in attempts] == ["local", None]
