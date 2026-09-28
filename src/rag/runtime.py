from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from src.actions.gateway import VehicleActionGateway
from src.agents.classifier import (
    GoogleIntentClassifier,
    IntentClassifier,
    LocalIntentClassifier,
    OpenAIIntentClassifier,
    RulesIntentClassifier,
)
from src.history.sqlite import SQLiteConversationHistory
from src.rag.generator import (
    ExtractiveHandbookGenerator,
    GoogleHandbookGenerator,
    HandbookGenerator,
    LocalHandbookGenerator,
    OpenAIHandbookGenerator,
)
from src.rag.retrieval_lexical import LexicalHandbookRetriever
from src.rag.schemas import RetrievedChunk
from src.rag.sqlite_store import SQLiteHandbookRetriever
from src.vivi.config import Settings, get_settings


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


def create_services(
    settings: Settings | None = None,
    *,
    retrieval_mode: str = "sqlite",
    provider: str = "auto",
) -> HandbookServices:
    config = settings or get_settings()
    if retrieval_mode == "sqlite":
        retriever = SQLiteHandbookRetriever(config.rag_handbook_db, final_k=config.rag_final_k)
    elif retrieval_mode == "lexical":
        retriever = LexicalHandbookRetriever(config.rag_data_dir, final_k=config.rag_final_k)
    elif retrieval_mode == "hybrid":
        from src.rag.embeddings.google import GoogleEmbeddingProvider
        from src.rag.rerankers.hybrid import HybridReranker
        from src.rag.retrieval import HandbookRetriever
        from src.rag.vectorstores.chroma import ChromaHandbookStore

        manifest_path = (
            config.rag_data_dir
            / "index"
            / config.rag_default_vehicle_model.lower()
            / str(config.rag_default_model_year)
            / config.rag_default_locale.lower()
            / "manifest.json"
        )
        if not manifest_path.exists():
            raise RuntimeError("Chroma index is not complete; run build_index or use --retrieval lexical")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete":
            raise RuntimeError("Chroma index is incomplete; resume build_index or use --retrieval lexical")
        embeddings = GoogleEmbeddingProvider(
            config.google_api_key,
            config.rag_embedding_model,
            config.rag_embedding_dimensions,
        )
        store = ChromaHandbookStore(config.chroma_persist_dir, config.rag_collection_name)
        retriever = HandbookRetriever(
            store,
            embeddings,
            HybridReranker(),
            retrieval_k=config.rag_retrieval_k,
            final_k=config.rag_final_k,
            max_cosine_distance=config.rag_max_cosine_distance,
        )
    else:
        raise ValueError(f"unsupported retrieval mode: {retrieval_mode}")
    selected = provider
    if selected == "auto":
        selected = "google" if config.google_api_key else "rules"
    if selected == "google":
        if not config.google_api_key:
            raise RuntimeError("GOOGLE_API_KEY is required for the google handbook graph")
        generator = GoogleHandbookGenerator(config.google_api_key, config.rag_generation_model)
        classifier = GoogleIntentClassifier(config.google_api_key, config.google_model)
    elif selected == "local":
        generator = LocalHandbookGenerator(config)
        classifier = LocalIntentClassifier(config)
    elif selected == "openai":
        if not config.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the openai handbook graph")
        generator = OpenAIHandbookGenerator(config)
        classifier = OpenAIIntentClassifier(config)
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
    )
