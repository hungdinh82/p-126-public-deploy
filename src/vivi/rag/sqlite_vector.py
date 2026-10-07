from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from src.vivi.rag.embeddings.base import EmbeddingProvider
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.rag.sqlite_store import SQLiteHandbookStore


class SQLiteVectorRetriever:
    """Scoped exact cosine + independent FTS5 retrieval, merged using RRF.

    At VF8's current scale the float32 matrix fits in a few MB. SQLite owns
    persistence; NumPy scans a read-only scoped matrix, avoiding ARM-specific
    extensions and an additional database daemon. No cloud calls are made.
    """

    def __init__(self, path: Path | str, embeddings: EmbeddingProvider, *, retrieval_k: int = 12,
                 final_k: int = 5, mode: str = "hybrid", min_similarity: float = 0.0):
        self.path = Path(path)
        self.store = SQLiteHandbookStore(path, read_only=True)
        self.embeddings = embeddings
        self.retrieval_k = retrieval_k
        self.final_k = final_k
        self.mode = mode
        self.min_similarity = min_similarity
        self._cache: dict[tuple[str, int, str], tuple[list[RetrievedChunk], object]] = {}
        self._lock = threading.Lock()
        with self.store._connect() as connection:
            row = connection.execute("SELECT value FROM vector_manifest WHERE key = 'manifest'").fetchone()
            self.manifest = json.loads(row[0]) if row else {}
            chunk_count = connection.execute("SELECT count(*) FROM handbook_chunks").fetchone()[0]
            vector_count = connection.execute("SELECT count(*) FROM handbook_vectors").fetchone()[0]
        if self.manifest.get("status") != "complete":
            raise ValueError("Local vector index is incomplete")
        if self.manifest.get("embedding_fingerprint") != getattr(embeddings, "fingerprint", None):
            raise ValueError("Embedding model differs from index; rebuild the local index")
        if vector_count != chunk_count or chunk_count != self.manifest.get("chunks", chunk_count):
            raise ValueError("Vector index does not cover the complete handbook")
        if self.mode not in {"hybrid", "vector"}:
            raise ValueError("SQLite vector mode must be hybrid or vector")

    def _load(self, scope: tuple[str, int, str]):
        import numpy as np

        with self._lock:
            if scope not in self._cache:
                with self.store._connect() as connection:
                    rows = connection.execute(
                        "SELECT c.*, 0 AS rank, v.embedding FROM handbook_chunks c "
                        "JOIN handbook_vectors v ON v.source_id = c.source_id "
                        "WHERE c.vehicle_model = ? AND c.model_year = ? AND c.locale = ? ORDER BY c.source_id",
                        scope,
                    ).fetchall()
                chunks = [self.store._to_retrieved(row) for row in rows]
                matrix = (np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in rows])
                          if rows else np.empty((0, self.manifest["dimensions"]), dtype=np.float32))
                if matrix.shape[1] != self.manifest["dimensions"] or not np.isfinite(matrix).all():
                    raise ValueError("Corrupt vector index")
                if len(matrix) and not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-4):
                    raise ValueError("Document vectors must have unit norm")
                self._cache[scope] = chunks, matrix
            return self._cache[scope]

    def retrieve(self, query: str, vehicle_model: str, model_year: int, locale: str) -> list[RetrievedChunk]:
        import numpy as np

        scope = vehicle_model.upper(), model_year, locale.lower()
        chunks, matrix = self._load(scope)
        if not chunks:
            return []
        vector = np.asarray(self.embeddings.embed_query(query), dtype=np.float32)
        if vector.shape != (self.manifest["dimensions"],) or not np.isfinite(vector).all():
            raise ValueError("Query embedding has invalid dimensions or values")
        vector /= max(float(np.linalg.norm(vector)), 1e-12)
        # Avoid activating a large BLAS thread pool for this small matrix.
        scores = np.einsum("ij,j->i", matrix, vector, optimize=False)
        order = np.argsort(-scores, kind="stable")[:self.retrieval_k]
        candidates: dict[str, RetrievedChunk] = {}
        for rank, index in enumerate(order, 1):
            if float(scores[index]) < self.min_similarity:
                continue
            chunk = chunks[int(index)].model_copy(deep=True)
            chunk.semantic_distance = 1 - float(scores[index])
            chunk.fused_score = 1 / (60 + rank)
            candidates[chunk.source_id] = chunk
        if self.mode == "hybrid":
            lexical = self.store.search(query, vehicle_model=scope[0], model_year=scope[1], locale=scope[2],
                                        limit=self.retrieval_k)
            by_id = {chunk.source_id: i for i, chunk in enumerate(chunks)}
            for rank, chunk in enumerate(lexical, 1):
                index = by_id.get(chunk.source_id)
                if index is None or float(scores[index]) < self.min_similarity:
                    continue
                if chunk.source_id not in candidates:
                    chunk.semantic_distance = 1 - float(scores[index])
                    chunk.fused_score = 0
                    candidates[chunk.source_id] = chunk
                item = candidates[chunk.source_id]
                item.lexical_score = chunk.lexical_score
                item.fused_score += 1 / (60 + rank)
        return sorted(candidates.values(), key=lambda chunk: (-chunk.fused_score, chunk.source_id))[:self.final_k]


def write_vectors(connection: sqlite3.Connection, source_ids: list[str], vectors: list[list[float]], dimensions: int):
    import numpy as np

    if len(source_ids) != len(vectors):
        raise ValueError("Embedding batch count mismatch")
    for source_id, vector in zip(source_ids, vectors, strict=True):
        values = np.asarray(vector, dtype="<f4")
        norm = float(np.linalg.norm(values))
        if values.shape != (dimensions,) or not np.isfinite(values).all() or norm < 1e-12:
            raise ValueError("Invalid document vector")
        connection.execute("INSERT INTO handbook_vectors(source_id, embedding) VALUES (?, ?)",
                           (source_id, (values / norm).astype("<f4").tobytes()))
