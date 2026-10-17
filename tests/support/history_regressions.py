"""Shared component fixtures extracted from historical scenario modules."""

from __future__ import annotations

from src.vivi.rag.schemas import RetrievedChunk


def chunk(source_id, content, section):
    return RetrievedChunk(
        source_id=source_id,
        document_id="manual",
        source_url="manual://test",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter=section,
        section_path=[section],
        content_type="paragraph",
        content=content,
        checksum="fixture",
        chunk_index=0,
    )
