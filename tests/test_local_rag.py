from __future__ import annotations

import json
import socket
import sqlite3

import pytest

from eval.benchmark import fact_coverage, load_dataset, local_network_only
from src.vivi.agents.classifier import RulesIntentClassifier
from src.vivi.rag.schemas import HandbookChunk
from src.vivi.rag.sqlite_store import SQLiteHandbookStore
from src.vivi.rag.sqlite_vector import SQLiteVectorRetriever, write_vectors


class FakeEmbeddings:
    fingerprint = "test-model-v1"

    def embed_query(self, text):
        return [1.0, 0.0]


def chunk(source_id, content, vehicle="VF8"):
    return HandbookChunk(source_id=source_id, document_id="manual", source_url="https://example.test/manual",
                         vehicle_model=vehicle, model_year=2026, locale="vi_vn", chapter_id=1,
                         chapter="Pin", section_path=["Pin"], content_type="paragraph", content=content,
                         checksum=source_id, chunk_index=0)


def vector_database(tmp_path):
    path = tmp_path / "handbook.sqlite3"
    jsonl = tmp_path / "chunks.jsonl"
    # Import one scope per call as required by the corpus importer.
    store = SQLiteHandbookStore(path)
    for items in ([chunk("semantic", "Bộ tích điện nằm dưới sàn."),
                   chunk("lexical", "Áp suất lốp cần kiểm tra khi nguội.")],
                  [chunk("other-car", "Áp suất lốp VF9.", "VF9")]):
        jsonl.write_text("\n".join(item.model_dump_json() for item in items) + "\n", encoding="utf-8")
        store.import_jsonl(jsonl)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE handbook_vectors(source_id TEXT PRIMARY KEY, embedding BLOB)")
        connection.execute("CREATE TABLE vector_manifest(key TEXT PRIMARY KEY, value TEXT)")
        write_vectors(connection, ["semantic", "lexical", "other-car"], [[1, 0], [0, 1], [1, 0]], 2)
        connection.execute("INSERT INTO vector_manifest VALUES ('manifest', ?)",
                           (json.dumps({"status": "complete", "dimensions": 2,
                                        "embedding_fingerprint": "test-model-v1"}),))
    # Production artifacts are checkpointed before immutable read-only access.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


def test_hybrid_unions_independent_fts_and_vector_candidates_and_filters_scope(tmp_path):
    path = vector_database(tmp_path)
    retriever = SQLiteVectorRetriever(path, FakeEmbeddings(), retrieval_k=1, final_k=2)
    # No topic filter here: this test isolates independent vector/FTS union.
    # Topic filtering is separately tested with domain-specific questions.
    result = retriever.retrieve("kiểm tra khi nguội", "VF8", 2026, "vi_vn")
    assert {item.source_id for item in result} == {"semantic", "lexical"}
    assert all(item.vehicle_model == "VF8" for item in result)
    assert retriever.retrieve("áp suất lốp", "VF8", 2025, "vi_vn") == []


def test_index_identity_and_invalid_vector_fail_closed(tmp_path):
    path = vector_database(tmp_path)
    model = FakeEmbeddings()
    model.fingerprint = "different-model"
    with pytest.raises(ValueError, match="differs"):
        SQLiteVectorRetriever(path, model)
    with sqlite3.connect(path) as connection, pytest.raises(ValueError, match="Invalid"):
        write_vectors(connection, ["bad"], [[float("nan"), 0]], 2)


def test_questions_about_controls_and_safety_are_not_actions():
    classifier = RulesIntentClassifier()
    for query in ["Cách mở cửa sổ bên tài như thế nào?", "Phanh ABS hoạt động như thế nào?",
                  "VF8 có tối đa bao nhiêu túi khí?"]:
        assert classifier.classify(query, []).route == "handbook"
    assert classifier.classify("Đạp phanh giúp tôi", []).route == "unsupported"


def test_evaluation_blocks_network_and_checks_facts():
    with local_network_only(), socket.socket() as connection:
        with pytest.raises(RuntimeError, match="non-local"):
            connection.connect(("8.8.8.8", 443))
    assert fact_coverage("Dung lượng pin là 82 kWh", [["82"], ["kWh"]]) == 1
    assert fact_coverage("Dung lượng pin là 87 kWh", [["82"], ["kWh"]]) == 0.5


def test_golden_dataset_references_and_source_grouped_splits_are_valid():
    from pathlib import Path

    cases = load_dataset(Path("eval/golden_dataset.jsonl"), Path("data/handbooks/handbook-source.sqlite3"))
    assert len(cases) >= 70
    assert {case["split"] for case in cases} == {"dev", "test"}


def test_local_build_is_complete_and_failed_rebuild_preserves_published_index(tmp_path):
    from src.vivi.cli.build_local_index import build

    class BuilderEmbeddings(FakeEmbeddings):
        batch_size = 1
        dimensions = 2
        manifest = {"model": "fixture"}

        def token_count(self, text):
            return len(text.split())

        def embed_documents(self, texts):
            return [[1.0, 0.0] for _ in texts]

    source = tmp_path / "source.sqlite3"
    target = tmp_path / "index.sqlite3"
    jsonl = tmp_path / "source.jsonl"
    jsonl.write_text(chunk("pin", "Dung lượng pin là 82 kWh.").model_dump_json() + "\n")
    SQLiteHandbookStore(source).import_jsonl(jsonl)
    manifest = build(source, target, BuilderEmbeddings())
    assert manifest["status"] == "complete"
    assert manifest["source_chunks"] == manifest["chunks"] == 1
    assert SQLiteVectorRetriever(target, BuilderEmbeddings()).retrieve("pin", "VF8", 2026, "vi_vn")
    published = target.read_bytes()

    class FailingEmbeddings(BuilderEmbeddings):
        def embed_documents(self, texts):
            raise RuntimeError("inference failed")

    with pytest.raises(RuntimeError, match="inference failed"):
        build(source, target, FailingEmbeddings())
    assert target.read_bytes() == published
    with pytest.raises(RuntimeError, match="Cannot import into a vector artifact"):
        SQLiteHandbookStore(target).import_jsonl(jsonl)


def test_services_use_canonical_local_index_and_reject_cloud_before_initializing(tmp_path, monkeypatch):
    from src.vivi.config import Settings
    from src.vivi.providers import configured_llm_providers, llm_models
    from src.vivi.rag import runtime

    config = Settings(rag_retrieval_mode="sqlite_local", rag_handbook_db=vector_database(tmp_path),
                      rag_history_db=tmp_path / "history.sqlite3", rag_local_only=True,
                      openai_api_key="configured-but-disabled", local_llm_model="", llm_provider="rules")
    monkeypatch.setattr(runtime, "local_embeddings", lambda *args: FakeEmbeddings())
    services = runtime.create_services(config)
    assert isinstance(services.retriever, SQLiteVectorRetriever)
    assert services.retriever.retrieve("áp suất lốp", "VF8", 2026, "vi_vn")
    assert set(llm_models(config)) == {"rules", "local"}
    assert configured_llm_providers(config) == ["rules"]
    with pytest.raises(RuntimeError, match="Offline RAG"):
        runtime.create_services(config, provider="google")
    with pytest.raises(RuntimeError, match="Offline RAG"):
        runtime.create_services(config, provider="openai")
