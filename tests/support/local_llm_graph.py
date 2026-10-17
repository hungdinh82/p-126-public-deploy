"""Shared component fixtures extracted from historical scenario modules."""

from __future__ import annotations

from src.vivi.rag.schemas import RetrievedChunk


def _retrieved_chunk() -> RetrievedChunk:
    return RetrievedChunk(
        source_id="vf-local-charge",
        document_id="VF8-2026-vi_vn-1",
        source_url="manual://vf8/2026/charge",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Sạc xe",
        section_path=["Pin", "Sạc AC"],
        content_type="paragraph",
        content="Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
        checksum="local-test",
        chunk_index=0,
    )


class _Retriever:
    def retrieve(self, query, vehicle_model, model_year, locale):
        assert query == "Sạc AC thế nào?"
        assert (vehicle_model, model_year, locale) == ("VF8", 2026, "vi_vn")
        return [_retrieved_chunk()]
