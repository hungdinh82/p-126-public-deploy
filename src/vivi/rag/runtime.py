from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from src.vivi.agents.classifier import (
    IntentClassifier,
    LocalIntentClassifier,
    OpenAIIntentClassifier,
    OpenRouterIntentClassifier,
    RulesIntentClassifier,
)
from src.vivi.config import Settings, get_settings
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.memory.sqlite import SQLiteLongTermMemory
from src.vivi.rag.generator import (
    ExtractiveHandbookGenerator,
    HandbookGenerator,
    LocalHandbookGenerator,
    OpenAIHandbookGenerator,
    OpenRouterHandbookGenerator,
)
from src.vivi.rag.retrieval_lexical import LexicalHandbookRetriever
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.rag.sqlite_store import SQLiteHandbookRetriever
from src.vivi.vehicle.gateway import VehicleActionGateway


class RetrieverPort(Protocol):
    def retrieve(
        self, query: str, vehicle_model: str, model_year: int, locale: str
    ) -> list[RetrievedChunk]: ...


@dataclass
class HandbookServices:
    retriever: RetrieverPort
    generator: HandbookGenerator
    history: SQLiteConversationHistory
    history_turns: int = 6
    classifier: IntentClassifier | None = None
    action_gateway: VehicleActionGateway | None = None
    memory: SQLiteLongTermMemory | None = None
    memory_profile_id: str = "default"
    memory_context_limit: int = 8
    memory_context_max_characters: int = 2000


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
        selected = "local" if config.local_llm_model else "rules"
    if selected == "google":
        raise ValueError("Gemini RAG has been removed; use rules or a local LLM")
    if config.rag_local_only and selected not in {"rules", "local"}:
        raise RuntimeError("Offline RAG permits only rules or a self-hosted local LLM")
    if retrieval_mode == "sqlite":
        retriever = SQLiteHandbookRetriever(config.rag_handbook_db, final_k=config.rag_final_k)
    elif retrieval_mode == "sqlite_local":
        from src.vivi.rag.sqlite_vector import SQLiteVectorRetriever

        embeddings = local_embeddings(config.rag_local_embedding_dir, config.rag_embedding_threads,
                                      config.rag_embedding_batch_size)
        retriever = SQLiteVectorRetriever(config.rag_handbook_db, embeddings, retrieval_k=config.rag_retrieval_k,
                                         final_k=config.rag_final_k, min_similarity=config.rag_local_min_similarity)
    elif retrieval_mode == "lexical":
        retriever = LexicalHandbookRetriever(config.rag_data_dir, final_k=config.rag_final_k)
    else:
        raise ValueError(f"unsupported retrieval mode: {retrieval_mode}")
    if selected == "local":
        generator = LocalHandbookGenerator(config)
        classifier = LocalIntentClassifier(config)
    elif selected == "openai":
        if not config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the openai handbook graph")
        generator = OpenAIHandbookGenerator(config)
        classifier = OpenAIIntentClassifier(config)
    elif selected == "openrouter":
        if not config.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required for the openrouter handbook graph")
        generator = OpenRouterHandbookGenerator(config)
        classifier = OpenRouterIntentClassifier(config)
    elif selected == "rules":
        generator = ExtractiveHandbookGenerator()
        classifier = RulesIntentClassifier()
    else:
        raise ValueError(f"unsupported generation provider: {selected}")
    action_gateway = VehicleActionGateway()
    history = SQLiteConversationHistory(config.rag_history_db)
    return HandbookServices(
        retriever=retriever,
        generator=generator,
        history=history,
        history_turns=config.rag_history_turns,
        classifier=classifier,
        action_gateway=action_gateway,
        memory=SQLiteLongTermMemory(config.memory_path) if config.memory_enabled else None,
        memory_profile_id=config.memory_profile_id,
        memory_context_limit=config.memory_context_limit,
        memory_context_max_characters=config.memory_context_max_characters,
    )
