"""One-time migration: preserve recordings/effects, abandon legacy task authority."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

WORKFLOW_VERSION = 3


def migrate_runtime(store, *, databases=()):
    with store.connect() as db:
        if db.execute("SELECT 1 FROM runtime_schema WHERE version=?", (WORKFLOW_VERSION,)).fetchone():
            return []
    root = store.path.parent / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True, exist_ok=True)
    backups = []
    for path in dict.fromkeys([store.path, *map(Path, databases)]):
        if not path.is_file():
            continue
        suffix = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:8]
        destination = root / f"{path.stem}-{suffix}{path.suffix}"
        with sqlite3.connect(path) as source, sqlite3.connect(destination) as backup:
            source.backup(backup)
        backups.append(str(destination))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM runtime_schema WHERE version=?", (WORKFLOW_VERSION,)).fetchone():
            db.execute("UPDATE dialogue_sessions SET task=NULL,revision=revision+1")
            db.execute("DELETE FROM suspended_tasks")
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='confirmation_grants'").fetchone()
            if exists:
                db.execute("UPDATE confirmation_grants SET used=1")
            db.execute("INSERT INTO runtime_schema VALUES (?)", (WORKFLOW_VERSION,))
    return backups
