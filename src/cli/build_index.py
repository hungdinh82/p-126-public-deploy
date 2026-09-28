from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from src.ingestion.crawler import CrawlTarget
from src.ingestion.indexer import HandbookIndexer
from src.ingestion.parser import HandbookParser
from src.rag.embeddings.google import GoogleEmbeddingProvider
from src.rag.vectorstores.chroma import ChromaHandbookStore
from src.vivi.config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Parse and index the crawled VF8 2026 manual")
    parser.add_argument("--model", default=settings.rag_default_vehicle_model)
    parser.add_argument("--year", type=int, default=settings.rag_default_model_year)
    parser.add_argument("--locale", default=settings.rag_default_locale)
    args = parser.parse_args()

    target = CrawlTarget(args.model, args.year, args.locale)
    chunks = HandbookParser(settings.rag_data_dir).parse(target)
    embeddings = GoogleEmbeddingProvider(
        settings.google_api_key,
        settings.rag_embedding_model,
        settings.rag_embedding_dimensions,
    )
    store = ChromaHandbookStore(settings.chroma_persist_dir, settings.rag_collection_name)
    report = HandbookIndexer(settings.rag_data_dir, store, embeddings).build(target)
    print(json.dumps({"parsed_chunks": len(chunks), **asdict(report)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
