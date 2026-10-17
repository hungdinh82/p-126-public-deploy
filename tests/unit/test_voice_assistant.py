from __future__ import annotations

import json

# Recent user utterances plus tool-boundary regressions. These are development
# cases, kept separate from the held-out handbook golden dataset.
from pathlib import Path

import pytest

from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.rag.generator import ExtractiveHandbookGenerator, validate_grounding
from src.vivi.rag.schemas import RetrievedChunk

CASES = [
    (case["query"], case["expected_route"], case["expected_intent"])
    for case in (
        json.loads(line) for line in (Path(__file__).parents[2] / "eval/voice_cases.jsonl").read_text().splitlines()
    )
]


class EmptyRetrieve:
    def retrieve(self, *args):
        return []


def source(content, section="Pin SDI", source_id="source"):
    return RetrievedChunk(
        source_id=source_id,
        document_id="VF8",
        source_url="manual://test",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Thông số",
        section_path=["Thông số", section],
        content_type="paragraph",
        content=content,
        checksum="test",
        chunk_index=0,
    )


def test_extractive_reads_only_requested_table_row_without_source_preamble():
    chunks = [
        source(
            "Mô tả Chi tiết Điện áp 400 V Dung lượng sử dụng 82 kWh\nMô tả Chi tiết\nĐiện áp 400 V\nDung lượng sử dụng 82 kWh\nLàm mát bằng chất lỏng"
        )
    ]
    answer = ExtractiveHandbookGenerator().generate("Dung lượng pin SDI bao nhiêu?", chunks, [])
    assert answer.answer == "Dung lượng sử dụng 82 kWh."
    assert "Theo mục" not in answer.answer and "400" not in answer.answer
    assert validate_grounding(answer, chunks) == (True, None)


def test_standard_tire_question_never_uses_spare_tire_specification():
    chunks = [
        source("(Ví dụ: Nếu kích thước lốp là P205/55, chiều cao bằng 55% chiều rộng.)", "Nhãn lốp", "example"),
        source("Kích thước T145/80 R19", "Thông số lốp dự phòng", "spare"),
        source("Kích thước 225/55 R19 (VF8 ECO) 245/45 R20 (VF8 PLUS)", "Thông số lốp xe", "standard"),
    ]
    answer = ExtractiveHandbookGenerator().generate("lốp xe có kích thước bao nhiêu", chunks, [])
    assert "T145" not in answer.answer and "ECO" in answer.answer and "PLUS" in answer.answer
    assert answer.claims[0].source_ids == ["standard"]
    assert (
        ExtractiveHandbookGenerator().generate("lốp xe có kích thước bao nhiêu", chunks[:2], []).insufficient_evidence
    )


def test_extractive_preserves_complete_negation_and_rejects_unrelated_evidence():
    chunk = source("Không sạc pin khi dây cáp bị hỏng. Không đặt vật kim loại lên bộ sạc.", "Hướng dẫn sạc")
    answer = ExtractiveHandbookGenerator().generate("Sạc pin khi dây cáp hỏng được không?", [chunk], [])
    assert "Không sạc pin khi dây cáp bị hỏng." in answer.answer
    assert validate_grounding(answer, [chunk]) == (True, None)
    assert ExtractiveHandbookGenerator().generate("Mật khẩu wifi là gì?", [chunk], []).insufficient_evidence


class NeverRetrieve:
    def retrieve(self, *args):
        raise AssertionError("Live status and cabin actions must not use RAG")


@pytest.mark.asyncio
async def test_async_model_router_preserves_live_tool_boundary():
    class ModelClient:
        async def agenerate_json(self, **kwargs):
            assert "điều hoà đang là bao nhiêu độ" in kwargs["user"]
            return '{"act":"request","actions":[{"capability":"vehicle.get_status"}]}'

    decision, tier, _ = await SemanticResolver({"google": ModelProvider("google", ModelClient(), 1)}).resolve(
        "điều hoà đang là bao nhiêu độ", SessionState(), requested="google"
    )
    assert tier == "google" and decision.actions[0].capability == "vehicle.get_status"


def test_model_prompt_gives_vehicle_persona_and_separates_spoken_answer_from_citations():
    from src.vivi.dialogue.planner import SEMANTIC_INSTRUCTION
    from src.vivi.rag.generator import StructuredAPIHandbookGenerator

    class CapturingClient:
        def generate_json(self, **kwargs):
            self.prompt = kwargs
            return json.dumps(
                {
                    "answer": "Dung lượng sử dụng là 82 kWh.",
                    "claims": [{"text": "Dung lượng sử dụng là 82 kWh.", "source_ids": ["S1"]}],
                }
            )

    client = CapturingClient()
    answer = StructuredAPIHandbookGenerator(client).generate(
        "Dung lượng pin SDI?", [source("Dung lượng sử dụng 82 kWh")], []
    )
    assert "tiếng nói của chiếc VF8" in client.prompt["system"]
    assert "Không đọc tên nguồn" in client.prompt["system"]
    assert "vehicle.get_status" in SEMANTIC_INSTRUCTION
    assert answer.citations and "S1" not in answer.answer


def test_overlong_model_answer_uses_complete_literal_fact_without_extra_model_call():
    from src.vivi.rag.generator import StructuredAPIHandbookGenerator

    class VerboseClient:
        calls = 0

        def generate_json(self, **kwargs):
            self.calls += 1
            return json.dumps(
                {"answer": "Thông tin dài về pin. " * 100, "claims": [{"text": "Thông tin pin.", "source_ids": ["S1"]}]}
            )

    client = VerboseClient()
    chunks = [source("Dung lượng sử dụng 82 kWh. Điện áp 400 V.")]
    answer = StructuredAPIHandbookGenerator(client).generate("Dung lượng pin SDI?", chunks, [])
    assert answer.answer == "Dung lượng sử dụng 82 kWh."
    assert client.calls == 1 and validate_grounding(answer, chunks) == (True, None)
