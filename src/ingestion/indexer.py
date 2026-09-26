from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.crawler import CrawlTarget
from src.rag.embeddings.base import EmbeddingProvider
from src.rag.schemas import HandbookChunk


@dataclass(frozen=True)
class IndexReport:
    total_chunks: int
    embedded_chunks: int
    reused_embeddings: int
    unchanged_chunks: int
    deleted_chunks: int


class HandbookIndexer:
    def __init__(
        self,
        data_dir: Path,
        store: Any,
        embeddings: EmbeddingProvider,
        *,
        batch_size: int = 32,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.store = store
        self.embeddings = embeddings
        self.batch_size = batch_size

    def build(self, target: CrawlTarget) -> IndexReport:
        path = self.data_dir / "parsed" / target.slug / "chunks.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"parsed chunks not found: {path}")
        parsed_manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
        index_manifest_path = self.data_dir / "index" / target.slug / "manifest.json"
        self._write_manifest(
            index_manifest_path,
            {
                "schema_version": 1,
                "status": "building",
                "started_at": datetime.now(UTC).isoformat(),
                "source_chunks_checksum": parsed_manifest["chunks_checksum"],
            },
        )
        chunks = [
            HandbookChunk.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        existing = self.store.existing_records()
        changed = [
            chunk
            for chunk in chunks
            if str(existing.get(chunk.source_id, {}).get("checksum", "")) != chunk.checksum
        ]

        # Parser improvements can change a source ID while leaving the exact
        # text intact. Reuse those vectors by checksum instead of billing the
        # remote embedding API again.
        reusable = {
            str(metadata.get("checksum")): metadata.get("_embedding")
            for metadata in existing.values()
            if metadata.get("checksum") and metadata.get("_embedding")
        }
        reused = [chunk for chunk in changed if chunk.checksum in reusable]
        for start in range(0, len(reused), self.batch_size):
            batch = reused[start : start + self.batch_size]
            self.store.upsert(batch, [reusable[chunk.checksum] for chunk in batch])

        to_embed = [chunk for chunk in changed if chunk.checksum not in reusable]

        try:
            for start in range(0, len(to_embed), self.batch_size):
                batch = to_embed[start : start + self.batch_size]
                vectors = self.embeddings.embed_documents([self._embedding_text(chunk) for chunk in batch])
                self.store.upsert(batch, vectors)
        except Exception as exc:
            self._write_manifest(
                index_manifest_path,
                {
                    "schema_version": 1,
                    "status": "incomplete",
                    "failed_at": datetime.now(UTC).isoformat(),
                    "source_chunks_checksum": parsed_manifest["chunks_checksum"],
                    "error": str(exc),
                },
            )
            raise

        current_ids = {chunk.source_id for chunk in chunks}
        stale = [
            source_id
            for source_id, metadata in existing.items()
            if source_id not in current_ids
            and str(metadata.get("vehicle_model", "")).upper() == target.vehicle_model.upper()
            and int(metadata.get("model_year", 0)) == target.model_year
            and str(metadata.get("locale", "")).lower() == target.locale.lower()
        ]
        self.store.delete(stale)
        report = IndexReport(
            total_chunks=len(chunks),
            embedded_chunks=len(to_embed),
            reused_embeddings=len(reused),
            unchanged_chunks=len(chunks) - len(changed),
            deleted_chunks=len(stale),
        )
        self._write_manifest(
            index_manifest_path,
            {
                "schema_version": 1,
                "status": "complete",
                "completed_at": datetime.now(UTC).isoformat(),
                "source_chunks_checksum": parsed_manifest["chunks_checksum"],
                "report": asdict(report),
            },
        )
        return report

    @staticmethod
    def _embedding_text(chunk: HandbookChunk) -> str:
        path = " > ".join(chunk.section_path)
        return f"Tài liệu xe {chunk.vehicle_model} {chunk.model_year}\nMục: {path}\n{chunk.content}"

    @staticmethod
    def _write_manifest(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
