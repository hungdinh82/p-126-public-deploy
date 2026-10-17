"""Shared component fixtures extracted from historical scenario modules."""

from __future__ import annotations

from src.vivi.rag.schemas import Citation, Claim, GroundedAnswer, RetrievedChunk


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        source_id="vf-test-source",
        document_id="VF8-2026-vi_vn-1",
        source_url="https://om.vinfastauto.com/vi_vn/detail?car=VF8&year=2026&lv2=1",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Hướng dẫn sạc",
        section_path=["Pin và Sạc", "Hướng dẫn sạc", "Sạc AC"],
        content_type="paragraph",
        content="Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
        checksum="abc",
        chunk_index=0,
        semantic_distance=0.2,
        fused_score=0.03,
    )


class FakeRetriever:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str, vehicle_model: str, model_year: int, locale: str):
        assert query
        self.queries.append(query)
        assert (vehicle_model, model_year, locale) == ("VF8", 2026, "vi_vn")
        return [_chunk()]


class FakeGenerator:
    def __init__(self, source_id: str = "vf-test-source") -> None:
        self.source_id = source_id

    def generate(self, query: str, chunks: list[RetrievedChunk], history: list[dict]):
        return GroundedAnswer(
            answer="Hãy dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
            claims=[Claim(text="Dừng xe an toàn trước khi sạc.", source_ids=[self.source_id])],
            citations=[
                Citation(
                    source_id=self.source_id,
                    title="Hướng dẫn sạc",
                    section_path=["Pin và Sạc", "Hướng dẫn sạc", "Sạc AC"],
                    source_url=chunks[0].source_url,
                )
            ],
        )
