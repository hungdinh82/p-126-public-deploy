from __future__ import annotations

import argparse
import json

from src.config import get_settings
from src.ingestion.crawler import CrawlTarget
from src.ingestion.parser import HandbookParser


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Parse a crawled VinFast owner manual for BM25")
    parser.add_argument("--model", default=settings.rag_default_vehicle_model)
    parser.add_argument("--year", type=int, default=settings.rag_default_model_year)
    parser.add_argument("--locale", default=settings.rag_default_locale)
    args = parser.parse_args()

    target = CrawlTarget(args.model, args.year, args.locale)
    chunks = HandbookParser(settings.rag_data_dir).parse(target)
    print(
        json.dumps(
            {
                "vehicle_model": target.vehicle_model,
                "model_year": target.model_year,
                "locale": target.locale,
                "parsed_chunks": len(chunks),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
