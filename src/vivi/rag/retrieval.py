from __future__ import annotations

from src.vivi.rag.embeddings.base import EmbeddingProvider
from src.vivi.rag.rerankers.hybrid import HybridReranker
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.rag.vectorstores.chroma import ChromaHandbookStore


class HandbookRetriever:
    def __init__(
        self,
        store: ChromaHandbookStore,
        embeddings: EmbeddingProvider,
        reranker: HybridReranker,
        *,
        retrieval_k: int = 12,
        final_k: int = 5,
        max_cosine_distance: float = 0.62,
    ) -> None:
        self.store = store
        self.embeddings = embeddings
        self.reranker = reranker
        self.retrieval_k = retrieval_k
        self.final_k = final_k
        self.max_cosine_distance = max_cosine_distance

    def retrieve(self, query: str, vehicle_model: str, model_year: int, locale: str) -> list[RetrievedChunk]:
        vector = self.embeddings.embed_query(query)
        candidates = self.store.query(
            vector,
            vehicle_model=vehicle_model,
            model_year=model_year,
            locale=locale,
            limit=self.retrieval_k,
        )
        relevant = [item for item in candidates if item.semantic_distance <= self.max_cosine_distance]
        return self.reranker.rerank(query, relevant, self.final_k)
