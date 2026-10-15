"""One-time migration: preserve recordings/effects, abandon legacy task authority."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

WORKFLOW_VERSION = 7


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
            db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_navigation_resets(session TEXT PRIMARY KEY, park INTEGER NOT NULL)"
            )
            navigation = db.execute("SELECT 1 FROM sqlite_master WHERE name='navigation_sessions'").fetchone()
            if navigation:
                db.execute(
                    "INSERT OR IGNORE INTO runtime_navigation_resets SELECT session, CASE WHEN json_extract(payload, '$.navigation_workspace.journey.status') IN ('running','charging') THEN 1 ELSE 0 END FROM navigation_sessions"
                )
                db.execute("DELETE FROM navigation_sessions")
            db.execute("INSERT INTO runtime_schema VALUES (?)", (WORKFLOW_VERSION,))
    return backups


def repair_logged_name(path):
    """CAS repair for the proven row-897 defect; never guess user preferences."""
    path = Path(path)
    if not path.is_file():
        return False
    expected_turn = "task:e69a22f9-c96c-4878-88bb-3eae50d7b98b:r2:step:3030dede-be42-47ca-991b-53a344fce3e9"
    with sqlite3.connect(path) as source:
        found = source.execute(
            "SELECT 1 FROM memory_items WHERE profile_id='default' AND key='display_name' AND value_json=? AND source_turn_id=? AND revision=1",
            ('"tôi"', expected_turn),
        ).fetchone()
        if not found:
            return False
        backup_path = path.with_name(
            path.stem + "-before-name-repair-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3"
        )
        with sqlite3.connect(backup_path) as backup:
            source.backup(backup)
        source.execute("BEGIN IMMEDIATE")
        source.execute(
            "DELETE FROM memory_items WHERE profile_id='default' AND key='display_name' AND value_json=? AND source_turn_id=? AND revision=1",
            ('"tôi"', expected_turn),
        )
    return True
