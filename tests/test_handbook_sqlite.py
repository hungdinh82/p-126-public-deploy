from __future__ import annotations

import json
from pathlib import Path

from src.vivi.rag.generator import ExtractiveHandbookGenerator, validate_grounding
from src.vivi.rag.schemas import HandbookChunk
from src.vivi.rag.sqlite_store import SQLiteHandbookRetriever, SQLiteHandbookStore


def _chunk(source_id: str, content: str, index: int = 0) -> HandbookChunk:
    return HandbookChunk(
        source_id=source_id,
        document_id="vf8-2026-vi_vn-1",
        source_url="https://example.test/manual/1",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Lốp và áp suất",
        section_path=["Bảo dưỡng", "Áp suất lốp"],
        content_type="paragraph",
        content=content,
        checksum=f"checksum-{source_id}",
        chunk_index=index,
    )


def _write_chunks(path: Path, chunks: list[HandbookChunk]) -> None:
    path.write_text(
        "\n".join(item.model_dump_json() for item in chunks) + "\n",
        encoding="utf-8",
    )


def test_sqlite_import_search_and_stale_cleanup(tmp_path):
    chunks_path = tmp_path / "chunks.jsonl"
    database = tmp_path / "handbook.sqlite3"
    _write_chunks(
        chunks_path,
        [
            _chunk("source-tire", "Kiểm tra áp suất lốp khi lốp đang nguội."),
            _chunk("source-charge", "Cổng sạc nằm phía bên trái của xe.", 1),
        ],
    )
    store = SQLiteHandbookStore(database)
    first = store.import_jsonl(chunks_path, source_checksum="v1")
    assert first.imported_chunks == 2
    results = store.search(
        "kiểm tra áp suất lốp",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
    )
    assert results[0].source_id == "source-tire"

    _write_chunks(chunks_path, [_chunk("source-tire", "Áp suất lốp phải kiểm tra khi nguội.")])
    second = store.import_jsonl(chunks_path, source_checksum="v2")
    assert second.deleted_chunks == 1
    assert store.search(
        "cổng sạc",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
    ) == []


def test_offline_extractive_answer_maps_every_claim_to_sqlite_source(tmp_path):
    chunks_path = tmp_path / "chunks.jsonl"
    database = tmp_path / "handbook.sqlite3"
    _write_chunks(chunks_path, [_chunk("source-tire", "Kiểm tra áp suất lốp khi lốp nguội.")])
    store = SQLiteHandbookStore(database)
    store.import_jsonl(chunks_path)
    chunks = store.search(
        "áp suất lốp",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
    )
    answer = ExtractiveHandbookGenerator().generate("Kiểm tra lốp thế nào?", chunks, [])
    assert validate_grounding(answer, chunks) == (True, None)
    assert json.loads(answer.model_dump_json())["claims"][0]["source_ids"] == ["source-tire"]


def test_runtime_retriever_is_read_only_and_missing_artifact_is_non_destructive(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    unavailable = SQLiteHandbookRetriever(missing)
    assert unavailable.available is False
    assert not missing.exists()
    assert unavailable.retrieve("sạc", "VF8", 2026, "vi_vn") == []

    chunks_path = tmp_path / "chunks.jsonl"
    database = tmp_path / "handbook.sqlite3"
    _write_chunks(chunks_path, [_chunk("source-tire", "Kiểm tra áp suất lốp khi nguội.")])
    SQLiteHandbookStore(database).import_jsonl(chunks_path)
    reader = SQLiteHandbookRetriever(database)
    assert reader.available is True
    assert reader.retrieve("áp suất lốp", "VF8", 2026, "vi_vn")[0].source_id == "source-tire"
