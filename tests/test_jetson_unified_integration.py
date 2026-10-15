"""Jetson routing preserves main's single workflow and vehicle effect ledger."""

import json
from types import SimpleNamespace

import httpx
import pytest

from jetson_ai.app import ServiceConfig, create_app
from src.vivi.ai_router import JetsonProviderRouter
from src.vivi.api.routes import turns
from src.vivi.config import Settings
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.structured_llm import StructuredChatClient
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.test_dialogue_v2 import ScriptedModel
from tests.test_long_term_memory import services as _services
from tests.test_rag import FakeRetriever

services = _services


@pytest.mark.asyncio
async def test_unified_semantic_tool_contract_through_authenticated_jetson_gateway():
    def engine(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        payload = json.loads(request.content)
        assert payload["model"] == "vivi-qwen3-1.7b"
        assert payload["tool_choice"] == "required"
        names = [item['function']['name'] for item in payload['tools']]
        assert 'interpret_turn' not in names
        name, arguments = ('climate_set_temperature', {'value_celsius': 24}) if 'climate_set_temperature' in names else (
            'dialogue_find_tools', {'query': 'climate.set_temperature'})
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "proposal", "type": "function", "function": {"name": name,
            "arguments": json.dumps(arguments)},
        }]}}]})

    gateway = create_app(ServiceConfig(token="s" * 32), transport=httpx.MockTransport(engine))
    client = StructuredChatClient(base_url="http://jetson.test/v1", api_key="s" * 32,
        model="vivi-qwen3-1.7b", timeout_seconds=1, transport=httpx.ASGITransport(app=gateway))
    resolver = SemanticResolver({"local": ModelProvider("local", client, 1)})
    proposal, served, _ = await resolver.resolve("đặt 24 độ", SessionState(), requested="local")
    assert served == "local"
    assert proposal.actions[0].capability == "climate.set_temperature"
    assert proposal.actions[0].arguments.value_celsius == 24


@pytest.mark.asyncio
async def test_router_uses_configured_providers_not_unified_graph_aliases(monkeypatch):
    config = Settings(jetson_ai_policy="prefer_local", jetson_ai_url="http://jetson.test",
                      jetson_ai_token="token", local_llm_model="vivi-qwen3-1.7b")
    aliases = {"rules", "local", "google", "openai", "openrouter"}
    runtime = SimpleNamespace(settings=config, orchestrator=SimpleNamespace(
        default_provider="local", graph_providers=aliases, semantic_providers={"rules", "local"},
        provider_errors={}))
    router = JetsonProviderRouter(config, transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    monkeypatch.setattr(turns, "runtime", runtime)
    monkeypatch.setattr(turns, "provider_router", router)
    assert await turns._provider("local") == "rules"


@pytest.mark.asyncio
async def test_jetson_failure_uses_cloud_in_same_workflow_and_executes_once(services, tmp_path):
    local = ScriptedModel([ConnectionError("Jetson disappeared after readiness probe")])
    cloud = ScriptedModel([{"act": "request", "actions": [{"capability": "climate.set_temperature",
                                                            "arguments": {"value_celsius": 24}}]}])
    ledger = RuntimeStore(tmp_path / "effects.sqlite3")
    gateway = VehicleActionGateway(services.action_gateway.vehicle, DurableConfirmations(ledger), ledger=ledger)
    services.action_gateway = gateway
    resolver = SemanticResolver({"local": ModelProvider("local", local, .2),
                                 "google": ModelProvider("google", cloud, .2)},
                                fallback_chains={"local": ["local", "google"]})
    graph = build_assistant_graph(services, ThreadedSqliteSaver(tmp_path / "conversation.sqlite3"),
                                 ledger, resolver=resolver)
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
    resolver = SemanticResolver({"local": ModelProvider("local", local, .2),
                                 "google": ModelProvider("google", cloud, .2)},
                                fallback_chains={"local": ["local"]})
    _, served, attempts = await resolver.resolve("xin chào", SessionState(), requested="local")
    assert served == "rules" and not cloud.prompts
    assert [a["provider"] for a in attempts] == ["local", "rules"]


@pytest.mark.asyncio
async def test_local_rag_generation_can_fall_back_to_cloud_without_repeating_tool(services, tmp_path):
    retrieved = []

    class LaptopRetriever(FakeRetriever):
        def retrieve(self, *args, **kwargs):
            retrieved.append(args)
            return super().retrieve(*args, **kwargs)

    class FailedGenerator:
        async def agenerate(self, *args):
            assert retrieved
            raise ConnectionError("Jetson handbook generation failed")

    local = ScriptedModel([{"act": "request", "actions": [
        {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}},
        {"capability": "manual.search", "arguments": {"query": "Cách kết nối cáp sạc AC?"}},
    ]}])
    ledger = RuntimeStore(tmp_path / "effects.sqlite3")
    services.action_gateway = VehicleActionGateway(services.action_gateway.vehicle,
                                                  DurableConfirmations(ledger), ledger=ledger)
    services.retriever = LaptopRetriever()
    cloud_generator = ExtractiveHandbookGenerator()
    resolver = SemanticResolver({"local": ModelProvider("local", local, .2)},
                                fallback_chains={"local": ["local", "google"]})
    graph = build_assistant_graph(services, ThreadedSqliteSaver(tmp_path / "conversation.sqlite3"), ledger,
        resolver=resolver, generators={"local": FailedGenerator(), "google": cloud_generator})
    try:
        result = await graph.ainvoke({"session_id": "driver", "turn_id": "one", "provider": "local",
                                     "input_text": "đặt 24 độ rồi tra cẩm nang"})
        assert result["citations"]
        assert result["diagnostics"]["generation_provider"] == "google"
        assert [a["outcome"] for a in result["diagnostics"]["generation_attempts"]] == ["fallback", "served"]
        assert len(local.prompts) == 1
        assert services.action_gateway.vehicle.state_for("driver").state_version == 1
        # The handbook evidence is retrieved on the Laptop before generation.
        assert len(retrieved) == 1
    finally:
        graph.close()
