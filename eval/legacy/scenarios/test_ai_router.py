from __future__ import annotations

import httpx
import pytest

from src.vivi.ai_router import JetsonProviderRouter
from src.vivi.config import Settings


def settings(policy: str, *, local_only: bool = False) -> Settings:
    return Settings(jetson_ai_policy=policy, jetson_ai_url="http://jetson.test:8789",
                    jetson_ai_token="token", local_llm_model="vivi-qwen3-1.7b",
                    rag_local_only=local_only)


@pytest.mark.anyio
async def test_router_selects_ready_gateway_without_token():
    def ready(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"contract_version": "1.0", "capabilities": {
            "llm": {"status": "ready", "model_id": "vivi-qwen3-1.7b"}}})

    config = settings("prefer_local")
    config.ai_server_auth_required = False
    config.jetson_ai_token = ""
    router = JetsonProviderRouter(config, transport=httpx.MockTransport(ready))
    assert await router.select("local", {"local", "rules"}) == "local"


@pytest.mark.anyio
async def test_router_selects_ready_jetson_before_graph():
    def ready(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"contract_version": "1.0", "capabilities": {
            "llm": {"status": "ready", "model_id": "vivi-qwen3-1.7b"}}})

    router = JetsonProviderRouter(settings("prefer_local"), transport=httpx.MockTransport(ready))
    assert await router.select("local", {"local", "rules", "google"}) == "local"
    assert await router.select("rules", {"local", "rules", "google"}) == "rules"


@pytest.mark.anyio
async def test_router_falls_back_to_cloud_or_rules_only_as_policy_allows():
    def busy(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"contract_version": "1.0", "capabilities": {
            "llm": {"status": "busy", "model_id": "vivi-qwen3-1.7b"}}})

    available = {"local", "rules", "google"}
    assert await JetsonProviderRouter(settings("prefer_local"), transport=httpx.MockTransport(busy)).select("local", available) == "google"
    assert await JetsonProviderRouter(settings("offline_only"), transport=httpx.MockTransport(busy)).select("local", available) == "rules"
    assert await JetsonProviderRouter(settings("allow_cloud", local_only=True), transport=httpx.MockTransport(busy)).select("local", available) == "rules"


@pytest.mark.anyio
async def test_router_treats_network_failure_as_unavailable():
    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Jetson offline", request=request)

    router = JetsonProviderRouter(settings("prefer_local"), transport=httpx.MockTransport(disconnected))
    assert await router.select("local", {"local", "rules", "openai"}) == "openai"
