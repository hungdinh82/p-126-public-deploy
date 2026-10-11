"""Public construction entry for the single conversation workflow."""
from __future__ import annotations

from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import create_services
from src.vivi.vehicle.gateway import VehicleActionGateway


def build_graph(services=None):
    services = services or create_services(provider="rules")
    services.action_gateway = services.action_gateway or VehicleActionGateway()
    path = getattr(services.history, "path", None)
    ledger = RuntimeStore(path.with_name(path.stem + "-effects.sqlite3")) if path else None
    services.action_gateway.ledger = ledger
    if ledger:
        services.action_gateway.confirmations = DurableConfirmations(ledger)
    saver = ThreadedSqliteSaver(path.with_name(path.stem + "-conversation-v3.sqlite3") if path else ":memory:")
    provider = services.generation_provider
    client = services.semantic_client
    provider = "model" if client and provider == "unknown" else provider
    resolver = SemanticResolver({provider: ModelProvider(provider, client, 12 if provider == "local" else 10)} if client else {})
    graph = build_assistant_graph(services, saver, ledger, resolver=resolver, generators={provider: services.generator, "rules": services.generator if not client else ExtractiveHandbookGenerator()})
    graph.default_provider = provider if client else "rules"
    return graph
