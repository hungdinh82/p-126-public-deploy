from __future__ import annotations

import json

# Recent user utterances plus tool-boundary regressions. These are development
# cases, kept separate from the held-out handbook golden dataset.
from pathlib import Path

import pytest

from src.vivi.agents.classifier import RulesIntentClassifier, StructuredAPIIntentClassifier
from src.vivi.agents.graph import build_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.rag.generator import ExtractiveHandbookGenerator, validate_grounding
from src.vivi.rag.runtime import HandbookServices
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.vehicle.gateway import VehicleActionGateway
from src.vivi.vehicle.memory import VehicleSimulator

CASES = [(case["query"], case["expected_route"], case["expected_intent"])
         for case in (json.loads(line) for line in
                      (Path(__file__).parents[1] / "eval/voice_cases.jsonl").read_text().splitlines())]



@pytest.mark.parametrize("text,route,intent", CASES)
def test_recent_utterances_route_to_the_right_capability(text, route, intent):
    decision = RulesIntentClassifier().classify_with_context(text, [], {"temperature_celsius": 23})
    assert (decision.route, decision.intent) == (route, intent)


def source(content, section="Pin SDI", source_id="source"):
    return RetrievedChunk(source_id=source_id, document_id="VF8", source_url="manual://test",
                          vehicle_model="VF8", model_year=2026, locale="vi_vn", chapter_id=1,
                          chapter="Thông số", section_path=["Thông số", section], content_type="paragraph",
                          content=content, checksum="test", chunk_index=0)


def test_extractive_reads_only_requested_table_row_without_source_preamble():
    chunks = [source("Mô tả Chi tiết Điện áp 400 V Dung lượng sử dụng 82 kWh\nMô tả Chi tiết\nĐiện áp 400 V\nDung lượng sử dụng 82 kWh\nLàm mát bằng chất lỏng")]
    answer = ExtractiveHandbookGenerator().generate("Dung lượng pin SDI bao nhiêu?", chunks, [])
    assert answer.answer == "Dung lượng sử dụng 82 kWh."
    assert "Theo mục" not in answer.answer and "400" not in answer.answer
    assert validate_grounding(answer, chunks) == (True, None)


def test_standard_tire_question_never_uses_spare_tire_specification():
    chunks = [source("(Ví dụ: Nếu kích thước lốp là P205/55, chiều cao bằng 55% chiều rộng.)", "Nhãn lốp", "example"),
              source("Kích thước T145/80 R19", "Thông số lốp dự phòng", "spare"),
              source("Kích thước 225/55 R19 (VF8 ECO) 245/45 R20 (VF8 PLUS)", "Thông số lốp xe", "standard")]
    answer = ExtractiveHandbookGenerator().generate("lốp xe có kích thước bao nhiêu", chunks, [])
    assert "T145" not in answer.answer and "ECO" in answer.answer and "PLUS" in answer.answer
    assert answer.claims[0].source_ids == ["standard"]
    assert ExtractiveHandbookGenerator().generate("lốp xe có kích thước bao nhiêu", chunks[:2], []).insufficient_evidence


def test_extractive_preserves_complete_negation_and_rejects_unrelated_evidence():
    chunk = source("Không sạc pin khi dây cáp bị hỏng. Không đặt vật kim loại lên bộ sạc.", "Hướng dẫn sạc")
    answer = ExtractiveHandbookGenerator().generate("Sạc pin khi dây cáp hỏng được không?", [chunk], [])
    assert "Không sạc pin khi dây cáp bị hỏng." in answer.answer
    assert validate_grounding(answer, [chunk]) == (True, None)
    assert ExtractiveHandbookGenerator().generate("Mật khẩu wifi là gì?", [chunk], []).insufficient_evidence


def test_relative_temperature_uses_observation_and_resolves_short_follow_up():
    classifier = RulesIntentClassifier()
    assert classifier.classify_with_context("Tôi hơi lạnh", [], {"temperature_celsius": 29}).arguments.value_celsius == 30
    assert classifier.classify("Tôi hơi lạnh", []).route == "clarify"
    history = [{"route": "action", "intent": "climate.set_temperature", "query": "Tôi hơi lạnh"}]
    assert classifier.classify_with_context("tăng thêm 1", history, {"temperature_celsius": 26}).arguments.value_celsius == 27
    assert classifier.classify_with_context("tăng thêm 1", [], {"temperature_celsius": 26}).route == "clarify"
    assert classifier.classify("Mở cửa sổ bên tài 50%", []).arguments.position_percent == 50
    assert classifier.classify("Phát nhạc thư giãn", []).arguments.media_query == "thư giãn"


class NeverRetrieve:
    def retrieve(self, *args):
        raise AssertionError("Live status and cabin actions must not use RAG")


@pytest.mark.parametrize("query,expected,absent", [
    ("điều hoà đang là bao nhiêu độ", "27 độ", "pin"),
    ("tình trạng pin hiện tại", "82 phần trăm", "Điều hoà"),
    ("còn đi được bao xa", "328 km", "nhiệt độ"),
    ("tình trạng áp suất lốp hiện tại", "250", "pin"),
])
def test_status_tool_reads_vehicle_and_speaks_only_requested_values(tmp_path, query, expected, absent):
    vehicle = VehicleSimulator()
    from src.vivi.domain.models import ActionProposal
    vehicle.execute_sync("voice", "prepare", ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": 27}))
    services = HandbookServices(NeverRetrieve(), ExtractiveHandbookGenerator(),
                                SQLiteConversationHistory(tmp_path / "history.sqlite3"),
                                classifier=RulesIntentClassifier(), action_gateway=VehicleActionGateway(vehicle))
    result = build_graph(services).invoke({"session_id": "voice", "turn_id": "status", "input_text": query})
    output = result["output"]
    assert output["intent"] == "vehicle.get_status" and output["status"] == "action_verified"
    assert expected in output["tts_text"] and absent not in output["tts_text"]
    assert output["vehicle_state"]["state_version"] == 1


def test_rule_fallback_preserves_tool_reads_and_negation():
    classifier = RulesIntentClassifier()
    assert classifier.classify("tình trạng pin hiện tại", []).intent == "vehicle.get_status"
    assert classifier.classify("Đừng mở cửa", []).route == "conversation"
    assert classifier.classify("Bạn có thể mở cửa sổ bên tài giúp tôi không?", []).intent == "window.set_position"


@pytest.mark.asyncio
async def test_async_model_router_preserves_live_tool_boundary():
    class ModelClient:
        async def agenerate_json(self, **kwargs):
            assert "điều hoà đang là bao nhiêu độ" in kwargs["user"]
            return '{"route":"action","intent":"vehicle.get_status"}'

    decision = await StructuredAPIIntentClassifier(ModelClient()).aclassify("điều hoà đang là bao nhiêu độ", [])
    assert decision.intent == "vehicle.get_status"


def test_model_prompt_gives_vehicle_persona_and_separates_spoken_answer_from_citations():
    from src.vivi.agents.prompts import CLASSIFIER_INSTRUCTION
    from src.vivi.rag.generator import StructuredAPIHandbookGenerator

    class CapturingClient:
        def generate_json(self, **kwargs):
            self.prompt = kwargs
            return json.dumps({"answer": "Dung lượng sử dụng là 82 kWh.",
                               "claims": [{"text": "Dung lượng sử dụng là 82 kWh.", "source_ids": ["S1"]}]})

    client = CapturingClient()
    answer = StructuredAPIHandbookGenerator(client).generate("Dung lượng pin SDI?", [source("Dung lượng sử dụng 82 kWh")], [])
    assert "tiếng nói của chiếc VF8" in client.prompt["system"]
    assert "Không đọc tên nguồn" in client.prompt["system"]
    assert "vehicle.get_status" in CLASSIFIER_INSTRUCTION
    assert answer.citations and "S1" not in answer.answer


def test_overlong_model_answer_uses_complete_literal_fact_without_extra_model_call():
    from src.vivi.rag.generator import StructuredAPIHandbookGenerator

    class VerboseClient:
        calls = 0

        def generate_json(self, **kwargs):
            self.calls += 1
            return json.dumps({"answer": "Thông tin dài về pin. " * 100,
                               "claims": [{"text": "Thông tin pin.", "source_ids": ["S1"]}]})

    client = VerboseClient()
    chunks = [source("Dung lượng sử dụng 82 kWh. Điện áp 400 V.")]
    answer = StructuredAPIHandbookGenerator(client).generate("Dung lượng pin SDI?", chunks, [])
    assert answer.answer == "Dung lượng sử dụng 82 kWh."
    assert client.calls == 1 and validate_grounding(answer, chunks) == (True, None)


def test_handbook_follow_up_does_not_bypass_unsafe_scope_guard(tmp_path):
    history = SQLiteConversationHistory(tmp_path / "history.sqlite3")
    history.save({"session_id": "follow", "turn_id": "1", "query": "Sạc pin thế nào?",
                  "answer": "Dừng xe an toàn.", "route": "handbook", "status": "answered"})
    services = HandbookServices(NeverRetrieve(), ExtractiveHandbookGenerator(), history)
    result = build_graph(services).invoke({"session_id": "follow", "turn_id": "2", "input_text": "Vậy cách hack pin cao áp?",
                                          "model_input": {"intent": "manual.search", "arguments": {"query": "Vậy cách hack pin cao áp?"}}})
    assert result["output"]["status"] == "out_of_scope"
    assert not result["output"]["citations"]
