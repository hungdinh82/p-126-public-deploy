from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class ConnectivityEventStore:
    """Small metadata-only audit store; payload text and credentials never enter it."""

    def __init__(self, path: Path, retention_days: int = 7) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.retention_seconds = retention_days * 86400
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS connectivity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at REAL NOT NULL,
                kind TEXT NOT NULL,
                component TEXT NOT NULL,
                outcome TEXT NOT NULL,
                metadata TEXT NOT NULL
            )"""
        )
        self._connection.commit()
        self.cleanup()

    def append(self, kind: str, component: str, outcome: str, **metadata: Any) -> None:
        safe = {
            key: value for key, value in metadata.items()
            if key in {"provider", "reason", "latency_ms", "previous", "profile", "override", "status"}
        }
        with self._lock:
            self._connection.execute(
                "INSERT INTO connectivity_events(occurred_at, kind, component, outcome, metadata) VALUES (?, ?, ?, ?, ?)",
                (time.time(), kind, component, outcome, json.dumps(safe, ensure_ascii=False)),
            )
            self._connection.commit()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT occurred_at, kind, component, outcome, metadata FROM connectivity_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"occurred_at": row[0], "kind": row[1], "component": row[2], "outcome": row[3], **json.loads(row[4])}
            for row in rows
        ]

    def cleanup(self) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM connectivity_events WHERE occurred_at < ?", (time.time() - self.retention_seconds,)
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()
