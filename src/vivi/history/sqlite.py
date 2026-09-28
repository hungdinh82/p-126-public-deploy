from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SQLiteConversationHistory:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handbook_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    status TEXT NOT NULL,
                    grounding_status TEXT NOT NULL,
                    citations_json TEXT NOT NULL,
                    vehicle_model TEXT NOT NULL,
                    model_year INTEGER NOT NULL,
                    locale TEXT NOT NULL,
                    timings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, turn_id)
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(handbook_turns)").fetchall()
            }
            migrations = {
                "intent": "TEXT",
                "route": "TEXT",
                "action_json": "TEXT NOT NULL DEFAULT '{}'",
                "execution_json": "TEXT NOT NULL DEFAULT '{}'",
                "confirmation_json": "TEXT NOT NULL DEFAULT '{}'",
                "vehicle_state_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE handbook_turns ADD COLUMN {name} {declaration}")

    def recent(self, session_id: str, limit: int = 6) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT query, answer, citations_json, created_at, intent, route, action_json,
                       execution_json, confirmation_json, vehicle_state_json
                FROM handbook_turns
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            {
                "query": row["query"],
                "answer": row["answer"],
                "citations": json.loads(row["citations_json"]),
                "intent": row["intent"],
                "route": row["route"],
                "action_proposal": json.loads(row["action_json"] or "{}") or None,
                "execution": json.loads(row["execution_json"] or "{}") or None,
                "confirmation": json.loads(row["confirmation_json"] or "{}") or None,
                "vehicle_state": json.loads(row["vehicle_state_json"] or "{}") or None,
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def save(self, state: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO handbook_turns (
                    session_id, turn_id, query, answer, status, grounding_status,
                    citations_json, vehicle_model, model_year, locale, timings_json, created_at,
                    intent, route, action_json, execution_json, confirmation_json,
                    vehicle_state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, turn_id) DO UPDATE SET
                    answer = excluded.answer,
                    status = excluded.status,
                    grounding_status = excluded.grounding_status,
                    citations_json = excluded.citations_json,
                    timings_json = excluded.timings_json,
                    intent = excluded.intent,
                    route = excluded.route,
                    action_json = excluded.action_json,
                    execution_json = excluded.execution_json,
                    confirmation_json = excluded.confirmation_json,
                    vehicle_state_json = excluded.vehicle_state_json
                """,
                (
                    state["session_id"],
                    state["turn_id"],
                    state.get("query", ""),
                    state.get("answer", ""),
                    state.get("status", "error"),
                    state.get("grounding_status", "unsupported"),
                    json.dumps(state.get("citations", []), ensure_ascii=False),
                    state.get("vehicle_model", "VF8"),
                    int(state.get("model_year", 2026)),
                    state.get("locale", "vi_vn"),
                    json.dumps(state.get("timings", {}), ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                    state.get("intent"),
                    state.get("route"),
                    json.dumps(state.get("action_proposal") or {}, ensure_ascii=False),
                    json.dumps(state.get("execution") or {}, ensure_ascii=False),
                    json.dumps(state.get("confirmation") or {}, ensure_ascii=False),
                    json.dumps(state.get("vehicle_state") or {}, ensure_ascii=False),
                ),
            )
