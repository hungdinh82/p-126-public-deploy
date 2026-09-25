from __future__ import annotations

import re
import unicodedata

from rank_bm25 import BM25Okapi

from src.rag.schemas import RetrievedChunk


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFD", text.lower())
    ascii_text = "".join(char for char in normalized if unicodedata.category(char) != "Mn").replace("đ", "d")
    return _WORD_RE.findall(ascii_text)


class HybridReranker:
    """Fuse semantic rank with local BM25 using reciprocal rank fusion."""

    def rerank(self, query: str, chunks: list[RetrievedChunk], limit: int) -> list[RetrievedChunk]:
        if not chunks:
            return []
        corpus = [_tokens(f"{' '.join(chunk.section_path)} {chunk.content}") for chunk in chunks]
        lexical_scores = BM25Okapi(corpus).get_scores(_tokens(query))
        lexical_order = sorted(range(len(chunks)), key=lambda index: lexical_scores[index], reverse=True)
        lexical_rank = {index: rank for rank, index in enumerate(lexical_order, start=1)}
        for semantic_rank, (index, chunk) in enumerate(zip(range(len(chunks)), chunks), start=1):
            chunk.lexical_score = float(lexical_scores[index])
            chunk.fused_score = 1 / (60 + semantic_rank) + 1 / (60 + lexical_rank[index])
        return sorted(chunks, key=lambda item: item.fused_score, reverse=True)[:limit]
