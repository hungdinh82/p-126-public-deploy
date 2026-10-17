"""Shared component fixtures extracted from historical scenario modules."""

import json
from dataclasses import replace

from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.support.long_term_memory import services as _services

services = _services


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    async def agenerate_json(self, **kwargs):
        if kwargs.get("schema_name") == "destination_choice":
            return json.dumps({"mode": "clarify", "handle": None, "reason": "ambiguous"})
        self.prompts.append(kwargs["user"])
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def build(services, tmp_path, model=None, store=None, local=None):
    store = store or RuntimeStore(tmp_path / "runtime.sqlite3")
    gateway = VehicleActionGateway(services.action_gateway.vehicle, DurableConfirmations(store), ledger=store)
    runtime = replace(services, action_gateway=gateway, generation_provider="extractive")
    providers = {"google": ModelProvider("google", model, 0.2)} if model else {}
    if local:
        providers["local"] = ModelProvider("local", local, 0.2)
    resolver = SemanticResolver(providers, breaker_seconds=0.02)
    saver = ThreadedSqliteSaver(tmp_path / "checkpoints.sqlite3")
    graph = build_assistant_graph(runtime, saver, store, resolver=resolver)
    graph.default_provider = "google"
    return graph, store, gateway


async def invoke(graph, text, turn):
    return await graph.ainvoke({"session_id": "driver", "turn_id": turn, "input_text": text})
