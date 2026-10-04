from __future__ import annotations

from src.vivi.rag.generator import normalize_answer, validate_grounding
from src.vivi.rag.schemas import Citation, Claim, GroundedAnswer, RetrievedChunk


def _chunk(source_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        source_id=source_id,
        document_id="VF8-2026-vi_vn-1",
        source_url="https://om.vinfastauto.com/vi_vn/detail?car=VF8&year=2026&lv2=1",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Hướng dẫn sạc",
        section_path=["Pin và Sạc"],
        content_type="paragraph",
        content="Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
        checksum="abc",
        chunk_index=0,
    )


def _answer(source_id: str, text: str = "Dừng xe an toàn.") -> GroundedAnswer:
    return GroundedAnswer(
        answer=text,
        claims=[Claim(text="Dừng xe an toàn.", source_ids=[source_id])],
        citations=[
            Citation(source_id=source_id, title="Sạc", section_path=["Pin và Sạc"], source_url="u")
        ],
    )


def test_missing_prefix_and_case_are_repaired_then_grounded():
    chunks = [_chunk("vf-c8730e538105314e8d64")]
    answer = normalize_answer(_answer("C8730e538105314e8d64"), chunks)
    assert answer.claims[0].source_ids == ["vf-c8730e538105314e8d64"]
    assert validate_grounding(answer, chunks) == (True, None)


def test_invented_source_is_still_rejected():
    chunks = [_chunk("vf-c8730e538105314e8d64")]
    answer = normalize_answer(_answer("vf-deadbeef"), chunks)
    valid, _ = validate_grounding(answer, chunks)
    assert not valid


def test_markdown_is_removed_for_speech():
    chunks = [_chunk("vf-a")]
    text = "Khi đèn sáng:\n\n1. **Dừng xe** an toàn.\n- Bơm lốp đúng áp suất."
    answer = normalize_answer(_answer("vf-a", text), chunks)
    assert answer.answer == "Khi đèn sáng: Dừng xe an toàn. Bơm lốp đúng áp suất."


def test_structured_generator_maps_short_labels_back_to_sources():
    import json

    from src.vivi.rag.generator import StructuredAPIHandbookGenerator

    class FakeClient:
        def generate_json(self, *, system, user, schema, schema_name):
            self.user = user
            return json.dumps({
                "answer": "Dừng xe an toàn trước khi sạc.",
                "claims": [{"text": "Dừng xe an toàn.", "source_ids": ["S2"]}],
                "citations": [],
                "insufficient_evidence": False,
            })

    client = FakeClient()
    chunks = [_chunk("vf-aaa"), _chunk("vf-bbb")]
    answer = StructuredAPIHandbookGenerator(client).generate("Sạc thế nào?", chunks, [])

    assert '"S2"' in client.user and "vf-bbb" not in client.user
    assert answer.claims[0].source_ids == ["vf-bbb"]
    assert [citation.source_id for citation in answer.citations] == ["vf-bbb"]
    assert validate_grounding(answer, chunks) == (True, None)
