from __future__ import annotations

import pytest

from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from tests.support.history_regressions import chunk as chunk


@pytest.mark.asyncio
async def test_async_model_interprets_even_an_obvious_control_request():
    class Model:
        async def agenerate_json(self, **kwargs):
            assert "Tăng điều hoà lên tối đa" in kwargs["user"]
            return '{"act":"request","actions":[{"capability":"climate.set_temperature","arguments":{"value_celsius":30}}]}'

    decision, tier, _ = await SemanticResolver({"google": ModelProvider("google", Model(), 1)}).resolve(
        "Tăng điều hoà lên tối đa", SessionState(), requested="google"
    )
    assert tier == "google" and decision.actions[0].arguments.value_celsius == 30


def test_definition_rejects_control_instructions_and_picks_actual_definition():
    generator = ExtractiveHandbookGenerator()
    unrelated = chunk(
        "buttons",
        "Nhấn nút ACC để kích hoạt kiểm soát hành trình. Khi ACC hoạt động, người lái chỉ kiểm soát vô lăng.",
        "Vận hành ACC",
    )
    definition = chunk(
        "definition",
        "Hệ thống kiểm soát hành trình thích ứng (ACC) là một hệ thống tự động điều chỉnh tốc độ để duy trì khoảng cách với xe phía trước.",
        "Hệ thống kiểm soát hành trình thích ứng",
    )
    answer = generator.generate("Cruise control là gì?", [unrelated, definition], [])
    assert "khoảng cách" in answer.answer and "Nhấn nút" not in answer.answer
    assert answer.claims[0].source_ids == ["definition"]
    assert generator.generate("Cruise control là gì?", [unrelated], []).insufficient_evidence


def test_charge_duration_never_uses_phone_charging_or_plugging_instructions():
    chunks = [
        chunk("phone", "Sạc không dây điện thoại mất khoảng 1 giờ.", "Điện thoại và sạc không dây"),
        chunk("plug", "Cắm súng sạc vào cổng sạc pin cho đến khi nghe tiếng tách.", "Pin và Sạc"),
    ]
    answer = ExtractiveHandbookGenerator().generate("Sạc AC mất bao lâu?", chunks, [])
    assert answer.insufficient_evidence


def test_question_counts_require_the_requested_object_not_shared_syllables():
    chunks = [chunk("brakes", "Hệ thống điều chỉnh lực phanh phân phối ra các cơ cấu phanh.", "Phanh xe")]
    assert (
        ExtractiveHandbookGenerator()
        .generate("Xe có bao nhiêu động cơ phản lực để bay?", chunks, [])
        .insufficient_evidence
    )


def test_wifi_password_instructions_are_not_the_users_password():
    chunks = [chunk("wifi", "Nhập mật khẩu mạng nếu được yêu cầu.", "Wi-Fi")]
    assert (
        ExtractiveHandbookGenerator().generate("Mật khẩu WiFi của xe của tôi là gì?", chunks, []).insufficient_evidence
    )


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
