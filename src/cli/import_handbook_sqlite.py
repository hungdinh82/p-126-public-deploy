from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.config import get_settings
from src.ingestion.crawler import CrawlTarget
from src.rag.sqlite_store import SQLiteHandbookStore


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import parsed handbook chunks into SQLite FTS5")
    parser.add_argument("--model", default=settings.rag_default_vehicle_model)
    parser.add_argument("--year", type=int, default=settings.rag_default_model_year)
    parser.add_argument("--locale", default=settings.rag_default_locale)
    parser.add_argument("--chunks", type=Path, help="Override the parsed chunks.jsonl path")
    parser.add_argument("--database", type=Path, default=settings.rag_handbook_db)
    args = parser.parse_args()

    target = CrawlTarget(args.model, args.year, args.locale)
    chunks_path = args.chunks or settings.rag_data_dir / "parsed" / target.slug / "chunks.jsonl"
    manifest_path = chunks_path.parent / "manifest.json"
    checksum = ""
    if manifest_path.exists():
        checksum = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("chunks_checksum", ""))
    report = SQLiteHandbookStore(args.database).import_jsonl(chunks_path, source_checksum=checksum)
    print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
