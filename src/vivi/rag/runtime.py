from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from src.vivi.config import Settings, get_settings
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.navigation import VietMapClient
from src.vivi.rag.generator import (
    ExtractiveHandbookGenerator,
    GoogleHandbookGenerator,
    HandbookGenerator,
    LocalHandbookGenerator,
    OpenAIHandbookGenerator,
    OpenRouterHandbookGenerator,
)
from src.vivi.rag.retrieval_lexical import LexicalHandbookRetriever
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.rag.sqlite_store import SQLiteHandbookRetriever
from src.vivi.vehicle.gateway import VehicleActionGateway

logger = logging.getLogger(__name__)


class RetrieverPort(Protocol):
    def retrieve(
        self, query: str, vehicle_model: str, model_year: int, locale: str
    ) -> list[RetrievedChunk]: ...


class FallbackRetriever:
    """Use the same handbook's FTS5 index when query embedding cannot run."""

    def __init__(self, primary: RetrieverPort, fallback: RetrieverPort):
        self.primary = primary
        self.fallback = fallback

    def retrieve(self, query: str, vehicle_model: str, model_year: int, locale: str) -> list[RetrievedChunk]:
        try:
            return self.primary.retrieve(query, vehicle_model, model_year, locale)
        except (ImportError, OSError, RuntimeError) as exc:
            logger.warning("Embedding unavailable; using handbook FTS5: %s", exc)
            return self.fallback.retrieve(query, vehicle_model, model_year, locale)


@dataclass
class HandbookServices:
    retriever: RetrieverPort
    generator: HandbookGenerator
    history: SQLiteConversationHistory
    history_turns: int = 6
    semantic_client: object | None = None
    action_gateway: VehicleActionGateway | None = None
    memory: SQLiteLongTermMemory | None = None
    memory_profile_id: str = "default"
    memory_context_limit: int = 8
    memory_context_max_characters: int = 2000
    generation_provider: str = "unknown"
    navigation: object | None = None
    energy_planner: object | None = None
    navigation_categories: dict | None = None
    charging_discovery: object | None = None
    conversation: object | None = None
    information: object | None = None


@lru_cache(maxsize=2)
def local_embeddings(directory: Path, threads: int, batch_size: int):
    from src.vivi.rag.embeddings.local import LocalE5EmbeddingProvider

    return LocalE5EmbeddingProvider(directory, threads=threads, batch_size=batch_size)


def create_services(
    settings: Settings | None = None,
    *,
    retrieval_mode: str | None = None,
    provider: str = "auto",
) -> HandbookServices:
    config = settings or get_settings()
    retrieval_mode = retrieval_mode or config.rag_retrieval_mode
    selected = provider
    if selected == "auto":
        selected = config.llm_provider
    if (config.rag_local_only or config.jetson_ai_policy == "offline_only") and selected not in {"rules", "local"}:
        raise RuntimeError("Offline RAG permits only rules or a self-hosted local LLM")
    if retrieval_mode == "sqlite":
        retriever = SQLiteHandbookRetriever(config.rag_handbook_db, final_k=config.rag_final_k)
    elif retrieval_mode == "sqlite_local":
        from src.vivi.rag.sqlite_vector import SQLiteVectorRetriever

        lexical = SQLiteHandbookRetriever(config.rag_handbook_db, final_k=config.rag_final_k)
        try:
            embeddings = local_embeddings(config.rag_local_embedding_dir, config.rag_embedding_threads,
                                          config.rag_embedding_batch_size)
        except (ImportError, OSError, RuntimeError) as exc:
            logger.warning("Embedding unavailable at startup; using handbook FTS5: %s", exc)
            retriever = lexical
        else:
            semantic = SQLiteVectorRetriever(config.rag_handbook_db, embeddings,
                                             retrieval_k=config.rag_retrieval_k,
                                             final_k=config.rag_final_k,
                                             min_similarity=config.rag_local_min_similarity)
            retriever = FallbackRetriever(semantic, lexical)
    elif retrieval_mode == "lexical":
        retriever = LexicalHandbookRetriever(config.rag_data_dir, final_k=config.rag_final_k)
    else:
        raise ValueError(f"unsupported retrieval mode: {retrieval_mode}")
    if selected == "local":
        generator = LocalHandbookGenerator(config)
    elif selected == "openai":
        if not config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the openai handbook graph")
        generator = OpenAIHandbookGenerator(config)
    elif selected == "openrouter":
        if not config.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for the openrouter handbook graph")
        generator = OpenRouterHandbookGenerator(config)
    elif selected == "google":
        if not config.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY (or GEMINI_API_KEY) is required for the google handbook graph")
        generator = GoogleHandbookGenerator(config)
    elif selected == "rules":
        generator = ExtractiveHandbookGenerator()
    else:
        raise ValueError(f"unsupported generation provider: {selected}")
    action_gateway = VehicleActionGateway()
    history = SQLiteConversationHistory(config.rag_history_db)
    return HandbookServices(
        retriever=retriever,
        generator=generator,
        history=history,
        history_turns=config.rag_history_turns,
        semantic_client=getattr(generator, "client", None),
        action_gateway=action_gateway,
        memory=SQLiteLongTermMemory(config.memory_path) if config.memory_enabled else None,
        memory_profile_id=config.memory_profile_id,
        memory_context_limit=config.memory_context_limit,
        memory_context_max_characters=config.memory_context_max_characters,
        generation_provider=selected,
        navigation=VietMapClient(config) if config.vietmap_api_key else None,
    )


# Retain the feature branch import name while using the unified runtime adapter.
EmbeddingFallbackRetriever = FallbackRetriever
