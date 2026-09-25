from __future__ import annotations

import argparse
import asyncio
import json

from src.config import get_settings
from src.ingestion.crawler import CrawlTarget, VinFastManualCrawler


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Crawl the public VinFast VF8 2026 owner manual")
    parser.add_argument("--model", default=settings.rag_default_vehicle_model)
    parser.add_argument("--year", type=int, default=settings.rag_default_model_year)
    parser.add_argument("--locale", default=settings.rag_default_locale)
    parser.add_argument("--force", action="store_true", help="Download chapters even when snapshots exist")
    args = parser.parse_args()

    target = CrawlTarget(args.model, args.year, args.locale)
    crawler = VinFastManualCrawler(settings.rag_data_dir)
    manifest = asyncio.run(crawler.crawl(target, force=args.force))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if manifest["failed_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
