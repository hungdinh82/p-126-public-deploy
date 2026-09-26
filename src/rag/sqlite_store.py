from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.rag.schemas import HandbookChunk, RetrievedChunk

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class SQLiteImportReport:
    imported_chunks: int
    deleted_chunks: int
    database_path: str


class SQLiteHandbookStore:
    """Compact FTS5 handbook artifact used by the PC and Jetson profiles."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS handbook_chunks (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE,
                    document_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    vehicle_model TEXT NOT NULL,
                    model_year INTEGER NOT NULL,
                    locale TEXT NOT NULL,
                    chapter_id INTEGER NOT NULL,
                    chapter TEXT NOT NULL,
                    section_path_json TEXT NOT NULL,
                    section_text TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_handbook_scope
                    ON handbook_chunks(vehicle_model, model_year, locale);

                CREATE TABLE IF NOT EXISTS handbook_imports (
                    id INTEGER PRIMARY KEY,
                    vehicle_model TEXT NOT NULL,
                    model_year INTEGER NOT NULL,
                    locale TEXT NOT NULL,
                    source_checksum TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    UNIQUE(vehicle_model, model_year, locale)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS handbook_chunks_fts USING fts5(
                    section_text,
                    content,
                    content='handbook_chunks',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );

                CREATE TRIGGER IF NOT EXISTS handbook_chunks_ai AFTER INSERT ON handbook_chunks BEGIN
                    INSERT INTO handbook_chunks_fts(rowid, section_text, content)
                    VALUES (new.id, new.section_text, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS handbook_chunks_ad AFTER DELETE ON handbook_chunks BEGIN
                    INSERT INTO handbook_chunks_fts(handbook_chunks_fts, rowid, section_text, content)
                    VALUES ('delete', old.id, old.section_text, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS handbook_chunks_au AFTER UPDATE ON handbook_chunks BEGIN
                    INSERT INTO handbook_chunks_fts(handbook_chunks_fts, rowid, section_text, content)
                    VALUES ('delete', old.id, old.section_text, old.content);
                    INSERT INTO handbook_chunks_fts(rowid, section_text, content)
                    VALUES (new.id, new.section_text, new.content);
                END;
                """
            )

    def import_jsonl(self, chunks_path: Path | str, *, source_checksum: str = "") -> SQLiteImportReport:
        path = Path(chunks_path)
        chunks = [
            HandbookChunk.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not chunks:
            raise ValueError(f"handbook chunks file is empty: {path}")
        scopes = {(item.vehicle_model.upper(), item.model_year, item.locale.lower()) for item in chunks}
        if len(scopes) != 1:
            raise ValueError("one import file must contain exactly one vehicle/model-year/locale scope")
        vehicle_model, model_year, locale = next(iter(scopes))
        current_ids = {item.source_id for item in chunks}
        with self._connect() as connection:
            existing = {
                row["source_id"]
                for row in connection.execute(
                    """
                    SELECT source_id FROM handbook_chunks
                    WHERE vehicle_model = ? AND model_year = ? AND locale = ?
                    """,
                    (vehicle_model, model_year, locale),
                )
            }
            for chunk in chunks:
                connection.execute(
                    """
                    INSERT INTO handbook_chunks (
                        source_id, document_id, source_url, vehicle_model, model_year, locale,
                        chapter_id, chapter, section_path_json, section_text, content_type,
                        content, checksum, chunk_index
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        document_id=excluded.document_id,
                        source_url=excluded.source_url,
                        vehicle_model=excluded.vehicle_model,
                        model_year=excluded.model_year,
                        locale=excluded.locale,
                        chapter_id=excluded.chapter_id,
                        chapter=excluded.chapter,
                        section_path_json=excluded.section_path_json,
                        section_text=excluded.section_text,
                        content_type=excluded.content_type,
                        content=excluded.content,
                        checksum=excluded.checksum,
                        chunk_index=excluded.chunk_index
                    """,
                    (
                        chunk.source_id,
                        chunk.document_id,
                        chunk.source_url,
                        vehicle_model,
                        model_year,
                        locale,
                        chunk.chapter_id,
                        chunk.chapter,
                        json.dumps(chunk.section_path, ensure_ascii=False),
                        " > ".join(chunk.section_path),
                        chunk.content_type,
                        chunk.content,
                        chunk.checksum,
                        chunk.chunk_index,
                    ),
                )
            stale = existing - current_ids
            if stale:
                placeholders = ",".join("?" for _ in stale)
                connection.execute(
                    f"DELETE FROM handbook_chunks WHERE source_id IN ({placeholders})",
                    tuple(stale),
                )
            connection.execute(
                """
                INSERT INTO handbook_imports (
                    vehicle_model, model_year, locale, source_checksum, chunk_count, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(vehicle_model, model_year, locale) DO UPDATE SET
                    source_checksum=excluded.source_checksum,
                    chunk_count=excluded.chunk_count,
                    imported_at=excluded.imported_at
                """,
                (
                    vehicle_model,
                    model_year,
                    locale,
                    source_checksum,
                    len(chunks),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return SQLiteImportReport(len(chunks), len(stale), str(self.path.resolve()))

    def search(
        self,
        query: str,
        *,
        vehicle_model: str,
        model_year: int,
        locale: str,
        limit: int = 5,
    ) -> list[RetrievedChunk]:
        tokens = _WORD_RE.findall(query.casefold())
        if not tokens:
            return []
        expression = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens[:24])
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*, bm25(handbook_chunks_fts, 3.0, 1.0) AS rank
                FROM handbook_chunks_fts AS f
                JOIN handbook_chunks AS c ON c.id = f.rowid
                WHERE handbook_chunks_fts MATCH ?
                  AND c.vehicle_model = ?
                  AND c.model_year = ?
                  AND c.locale = ?
                ORDER BY rank ASC, c.chunk_index ASC
                LIMIT ?
                """,
                (expression, vehicle_model.upper(), model_year, locale.lower(), limit),
            ).fetchall()
        return [self._to_retrieved(row) for row in rows]

    @staticmethod
    def _to_retrieved(row: sqlite3.Row) -> RetrievedChunk:
        return RetrievedChunk(
            source_id=row["source_id"],
            document_id=row["document_id"],
            source_url=row["source_url"],
            vehicle_model=row["vehicle_model"],
            model_year=row["model_year"],
            locale=row["locale"],
            chapter_id=row["chapter_id"],
            chapter=row["chapter"],
            section_path=json.loads(row["section_path_json"]),
            content_type=row["content_type"],
            content=row["content"],
            checksum=row["checksum"],
            chunk_index=row["chunk_index"],
            semantic_distance=0,
            lexical_score=max(0.0, -float(row["rank"])),
            fused_score=max(0.0, -float(row["rank"])),
        )


class SQLiteHandbookRetriever:
    def __init__(self, path: Path | str, *, final_k: int = 5) -> None:
        self.store = SQLiteHandbookStore(path)
        self.final_k = final_k

    def retrieve(
        self,
        query: str,
        vehicle_model: str,
        model_year: int,
        locale: str,
    ) -> list[RetrievedChunk]:
        return self.store.search(
            query,
            vehicle_model=vehicle_model,
            model_year=model_year,
            locale=locale,
            limit=self.final_k,
        )
