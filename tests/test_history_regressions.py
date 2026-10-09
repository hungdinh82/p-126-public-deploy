from __future__ import annotations

import pytest

from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.domain.models import VehicleState
from src.vivi.graphs import build_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.semantic_helpers import parse


def chunk(source_id, content, section):
    return RetrievedChunk(source_id=source_id, document_id="manual", source_url="manual://test",
                          vehicle_model="VF8", model_year=2026, locale="vi_vn", chapter_id=1,
                          chapter=section, section_path=[section], content_type="paragraph",
                          content=content, checksum="fixture", chunk_index=0)


@pytest.mark.parametrize("query,expected", [
    ("tăng điều hoà lên tối đa", 30),
    ("hạ nhiệt độ xuống thấp nhất", 16),
    ("tăng nhiệt độ thêm 20 độ", 30),
    ("tăng nhiệt độ lên 20 độ", 20),

])
def test_temperature_target_words_take_precedence_over_numeric_heuristics(query, expected):
    decision = parse(query, observation=VehicleState(temperature_celsius=23))
    assert decision.intent == "climate.set_temperature" and decision.resolved["value_celsius"] == expected


def test_rule_fallback_preserves_obvious_tool_and_dialogue_requests():
    for query, intent in [("Tôi hơi lạnh", "climate.set_temperature"),
                          ("Tăng điều hoà lên tối đa", "climate.set_temperature"),
                          ("Thống số pin hiện tại", "vehicle.get_status"),
                          ("Bạn có đang gặp vấn đề gì không?", "vehicle.get_status"),
                          ("bạn giúp gì được cho mình", "conversation.respond")]:
        assert parse(query, observation=VehicleState()).intent == intent


@pytest.mark.asyncio
async def test_async_model_interprets_even_an_obvious_control_request():
    class Model:
        async def agenerate_json(self, **kwargs):
            assert "Tăng điều hoà lên tối đa" in kwargs["user"]
            return '{"act":"request","actions":[{"capability":"climate.set_temperature","arguments":{"value_celsius":30}}]}'
    decision, tier, _ = await SemanticResolver({"google": ModelProvider("google", Model(), 1)}).resolve(
        "Tăng điều hoà lên tối đa", SessionState(), requested="google")
    assert tier == "google" and decision.actions[0].arguments.value_celsius == 30


def test_definition_rejects_control_instructions_and_picks_actual_definition():
    generator = ExtractiveHandbookGenerator()
    unrelated = chunk("buttons", "Nhấn nút ACC để kích hoạt kiểm soát hành trình. Khi ACC hoạt động, người lái chỉ kiểm soát vô lăng.", "Vận hành ACC")
    definition = chunk("definition", "Hệ thống kiểm soát hành trình thích ứng (ACC) là một hệ thống tự động điều chỉnh tốc độ để duy trì khoảng cách với xe phía trước.", "Hệ thống kiểm soát hành trình thích ứng")
    answer = generator.generate("Cruise control là gì?", [unrelated, definition], [])
    assert "khoảng cách" in answer.answer and "Nhấn nút" not in answer.answer
    assert answer.claims[0].source_ids == ["definition"]
    assert generator.generate("Cruise control là gì?", [unrelated], []).insufficient_evidence


def test_charge_duration_never_uses_phone_charging_or_plugging_instructions():
    chunks = [chunk("phone", "Sạc không dây điện thoại mất khoảng 1 giờ.", "Điện thoại và sạc không dây"),
              chunk("plug", "Cắm súng sạc vào cổng sạc pin cho đến khi nghe tiếng tách.", "Pin và Sạc")]
    answer = ExtractiveHandbookGenerator().generate("Sạc AC mất bao lâu?", chunks, [])
    assert answer.insufficient_evidence


def test_question_counts_require_the_requested_object_not_shared_syllables():
    chunks = [chunk("brakes", "Hệ thống điều chỉnh lực phanh phân phối ra các cơ cấu phanh.", "Phanh xe")]
    assert ExtractiveHandbookGenerator().generate("Xe có bao nhiêu động cơ phản lực để bay?", chunks, []).insufficient_evidence


def test_wifi_password_instructions_are_not_the_users_password():
    chunks = [chunk("wifi", "Nhập mật khẩu mạng nếu được yêu cầu.", "Wi-Fi")]
    assert ExtractiveHandbookGenerator().generate("Mật khẩu WiFi của xe của tôi là gì?", chunks, []).insufficient_evidence


def test_new_handbook_topic_does_not_inherit_previous_topic():
    from src.vivi.dialogue.parser import OfflineParser
    history = [{"route": "handbook", "query": "ADAS là gì?"}]
    query = "Còn áp suất lốp khuyến nghị?"
    assert OfflineParser.question_reference(query, history) == query
    assert "ADAS" in OfflineParser.question_reference("Vậy nó có thay thế người lái không?", history)


def test_charger_clarification_keeps_original_question(tmp_path):
    class Retriever:
        def retrieve(self, query, *args):
            self.query = query
            return []
    retriever = Retriever()
    history = SQLiteConversationHistory(tmp_path / "history.sqlite3")
    graph = build_graph(HandbookServices(retriever, ExtractiveHandbookGenerator(), history,
                                       generation_provider="rules"))
    graph.invoke({"session_id": "s", "input_text": "Cần bao lâu để sạc đầy?"})
    resumed = graph.invoke({"session_id": "s", "input_text": "DC"})
    assert "bao lâu" in retriever.query and "DC" in retriever.query
    saved = history.recent("s")[-1]
    assert saved["query"] == "DC"
    patch = saved["diagnostics"]["semantic"]["actions"][0]["arguments"]
    assert patch["charging_mode"] == "DC" and patch["query"] is None
    assert resumed["session"]["active"]["steps"][0]["draft"]["arguments"]["query"] == "Cần bao lâu để sạc đầy?"
    assert saved["diagnostics"]["generation_provider"] is None  # No evidence means no generation call.


def test_model_query_rewrite_cannot_erase_an_unsafe_original_request(tmp_path):
    class NoRetrieval:
        def retrieve(self, *args):
            raise AssertionError("Unsafe original must be stopped before retrieval")
    graph = build_graph(HandbookServices(NoRetrieval(), ExtractiveHandbookGenerator(),
                                       SQLiteConversationHistory(tmp_path / "history.sqlite3")))
    result = graph.invoke({"input_text": "Cách tháo pin cao áp", "model_input": {
        "route": "handbook", "intent": "manual.search", "arguments": {"query": "Sạc pin VF8"}
    }})
    assert result["output"]["status"] == "unsupported"


def test_evidence_gate_rejects_wrong_charging_domain_for_any_retriever(tmp_path):
    class Retriever:
        def retrieve(self, *args):
            return [chunk("phone", "Sạc không dây mất 1 giờ.", "Sạc không dây điện thoại")]
    services = HandbookServices(Retriever(), ExtractiveHandbookGenerator(),
                                SQLiteConversationHistory(tmp_path / "history.sqlite3"),
                                action_gateway=VehicleActionGateway())
    result = build_graph(services).invoke({"input_text": "Sạc AC mất bao lâu?"})
    assert result["output"]["citations"] == []
    assert result["output"]["status"] == "insufficient_evidence"


def test_history_migrates_old_database_without_losing_turns(tmp_path):
    import sqlite3

    path = tmp_path / "history.sqlite3"
    history = SQLiteConversationHistory(path)
    history.save({"session_id": "s", "turn_id": "t", "query": "Xin chào", "answer": "Mình đây."})
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE handbook_turns DROP COLUMN diagnostics_json")
    migrated = SQLiteConversationHistory(path)
    assert migrated.recent("s")[0]["answer"] == "Mình đây."
    assert migrated.recent("s")[0]["diagnostics"] == {}
