from __future__ import annotations

from pathlib import Path

from rank_bm25 import BM25Okapi

from src.vivi.ingestion.crawler import CrawlTarget
from src.vivi.rag.rerankers.hybrid import _tokens
from src.vivi.rag.schemas import HandbookChunk, RetrievedChunk


class LexicalHandbookRetriever:
    """Local full-corpus fallback when remote embeddings are unavailable."""

    def __init__(self, data_dir: Path, *, final_k: int = 5) -> None:
        self.data_dir = Path(data_dir)
        self.final_k = final_k
        self._cache_key: tuple[str, int, str] | None = None
        self._chunks: list[HandbookChunk] = []
        self._bm25: BM25Okapi | None = None

    def retrieve(self, query: str, vehicle_model: str, model_year: int, locale: str) -> list[RetrievedChunk]:
        self._load(vehicle_model, model_year, locale)
        if self._bm25 is None or not self._chunks:
            return []
        scores = self._bm25.get_scores(_tokens(query))
        order = sorted(range(len(self._chunks)), key=lambda index: scores[index], reverse=True)
        if not order or float(scores[order[0]]) <= 0:
            return []
        results: list[RetrievedChunk] = []
        seen_sections: set[str] = set()
        for index in order:
            score = float(scores[index])
            if score <= 0:
                break
            chunk = self._chunks[index]
            section = " > ".join(chunk.section_path)
            if section in seen_sections and len(results) >= 2:
                continue
            seen_sections.add(section)
            results.append(
                RetrievedChunk(
                    **chunk.model_dump(),
                    semantic_distance=0,
                    lexical_score=score,
                    fused_score=score,
                )
            )
            if len(results) >= self.final_k:
                break
        return results

    def _load(self, vehicle_model: str, model_year: int, locale: str) -> None:
        key = (vehicle_model.upper(), model_year, locale.lower())
        if key == self._cache_key:
            return
        target = CrawlTarget(*key)
        path = self.data_dir / "parsed" / target.slug / "chunks.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"parsed handbook not found: {path}; run build_index first")
        self._chunks = [
            HandbookChunk.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        corpus = [_tokens(f"{' '.join(chunk.section_path)} {chunk.content}") for chunk in self._chunks]
        self._bm25 = BM25Okapi(corpus)
        self._cache_key = key
