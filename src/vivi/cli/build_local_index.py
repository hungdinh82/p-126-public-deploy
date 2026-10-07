from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from src.vivi.rag.embeddings.local import LocalE5EmbeddingProvider, file_sha256
from src.vivi.rag.schemas import HandbookChunk
from src.vivi.rag.sqlite_store import SQLiteHandbookStore
from src.vivi.rag.sqlite_vector import write_vectors


def embedding_text(chunk: HandbookChunk) -> str:
    return f"{' > '.join(chunk.section_path)}\n{chunk.content}"


def split_chunk(chunk: HandbookChunk, embeddings: LocalE5EmbeddingProvider) -> list[HandbookChunk]:
    if embeddings.token_count("passage: " + embedding_text(chunk)) <= 480:
        return [chunk]
    # Word boundaries preserve Vietnamese characters and every source word.
    words = chunk.content.split()
    result = []
    start = 0
    while start < len(words):
        low, high = start + 1, len(words)
        end = start
        while low <= high:
            middle = (low + high) // 2
            part = chunk.model_copy(update={"content": " ".join(words[start:middle])})
            if embeddings.token_count("passage: " + embedding_text(part)) <= 480:
                end, low = middle, middle + 1
            else:
                high = middle - 1
        if end == start:
            raise ValueError(f"Section heading exceeds embedding budget: {chunk.source_id}")
        content = " ".join(words[start:end])
        result.append(chunk.model_copy(update={
            "source_id": f"{chunk.source_id}-p{len(result) + 1}",
            "content": content,
            "checksum": hashlib.sha256(content.encode()).hexdigest(),
        }))
        start = end if end == len(words) else max(start + 1, end - 24)
    return result


def build(source: Path, database: Path, embeddings: LocalE5EmbeddingProvider) -> dict:
    started = time.perf_counter()
    if source.resolve() == database.resolve():
        raise ValueError("Build into a separate artifact; source database must remain intact")
    store = SQLiteHandbookStore(source, read_only=True)
    with store._connect() as connection:
        source_chunks = [HandbookChunk.model_validate(store._to_retrieved(row).model_dump()) for row in
                         connection.execute("SELECT *, 0 AS rank FROM handbook_chunks ORDER BY id")]
    if not source_chunks:
        raise ValueError("Source handbook is empty")
    chunks = [part for chunk in source_chunks for part in split_chunk(chunk, embeddings)]
    scopes = {(chunk.vehicle_model, chunk.model_year, chunk.locale) for chunk in chunks}
    if len(scopes) != 1:
        raise ValueError("This builder accepts one handbook scope per artifact")
    database.parent.mkdir(parents=True, exist_ok=True)
    # A temporary sibling allows atomic publish; a failed build preserves the previous artifact.
    with tempfile.TemporaryDirectory(prefix="vivi-index-", dir=database.parent) as directory:
        temporary = Path(directory) / "handbook.sqlite3"
        jsonl = Path(directory) / "chunks.jsonl"
        jsonl.write_text("\n".join(chunk.model_dump_json() for chunk in chunks) + "\n", encoding="utf-8")
        SQLiteHandbookStore(temporary).import_jsonl(jsonl, source_checksum=file_sha256(source))
        with sqlite3.connect(temporary) as connection:
            connection.execute("CREATE TABLE handbook_vectors (source_id TEXT PRIMARY KEY REFERENCES handbook_chunks(source_id), embedding BLOB NOT NULL)")
            connection.execute("CREATE TABLE vector_manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for start in range(0, len(chunks), embeddings.batch_size):
                batch = chunks[start:start + embeddings.batch_size]
                vectors = embeddings.embed_documents([embedding_text(chunk) for chunk in batch])
                write_vectors(connection, [chunk.source_id for chunk in batch], vectors, embeddings.dimensions)
                if start % 100 == 0:
                    print(f"Embedded {min(start + len(batch), len(chunks))}/{len(chunks)}", flush=True)
            manifest = {
                "schema_version": 1, "status": "complete", "source_sha256": file_sha256(source),
                "embedding_fingerprint": embeddings.fingerprint, "embedding_model": embeddings.manifest,
                "dimensions": embeddings.dimensions, "source_chunks": len(source_chunks), "chunks": len(chunks),
                "scope": list(next(iter(scopes))), "chunking": {"max_tokens": 480, "overlap_words": 24},
                "build_seconds": round(time.perf_counter() - started, 2),
            }
            connection.execute("INSERT INTO vector_manifest VALUES ('manifest', ?)", (json.dumps(manifest),))
        with sqlite3.connect(temporary) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        temporary.replace(database)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed the complete handbook locally into an atomic SQLite artifact")
    parser.add_argument("--source", type=Path, default=Path("data/handbooks/handbook-source.sqlite3"))
    parser.add_argument("--database", type=Path, default=Path("data/handbooks/handbook.sqlite3"))
    parser.add_argument("--model-dir", type=Path, default=Path("models/multilingual-e5-small-int8"))
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args()
    model = LocalE5EmbeddingProvider(args.model_dir, threads=args.threads, batch_size=args.batch_size)
    print(json.dumps(build(args.source, args.database, model), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
