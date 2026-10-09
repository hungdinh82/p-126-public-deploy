import pytest

from src.vivi.graphs import build_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.rag.runtime import HandbookServices
from tests.test_rag import FakeGenerator, FakeRetriever


@pytest.mark.asyncio
async def test_unified_graph_has_one_durable_and_one_ephemeral_state(tmp_path):
    graph = build_graph(HandbookServices(FakeRetriever(), FakeGenerator(), SQLiteConversationHistory(tmp_path / 'history.db')))
    try:
        result = await graph.ainvoke({'input_text': 'Xin chào', 'session_id': 's', 'turn_id': 't'})
        assert result['output']['response_text'] == 'Mình đây, bạn cần gì thế?'
        assert {'event', 'session', 'turn', 'output'} <= result.keys()
        assert result['output']['diagnostics']['workflow_version'] == 'unified-3'
    finally:
        graph.close()
