"""Shared component fixtures extracted from historical scenario modules."""

from __future__ import annotations

import pytest

from src.vivi.graphs import build_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.vehicle.gateway import VehicleActionGateway


class NoRetrieval:
    def retrieve(self, *args):
        raise AssertionError("Memory requests must not retrieve handbook evidence")


@pytest.fixture
def services(tmp_path):
    return HandbookServices(
        NoRetrieval(),
        ExtractiveHandbookGenerator(),
        SQLiteConversationHistory(tmp_path / "history.sqlite3"),
        action_gateway=VehicleActionGateway(),
        memory=SQLiteLongTermMemory(tmp_path / "memory.sqlite3"),
    )


def invoke(services, text, session="driver", turn=None):
    state = {"input_text": text, "session_id": session}
    if turn:
        state["turn_id"] = turn
    return build_graph(services).invoke(state)
