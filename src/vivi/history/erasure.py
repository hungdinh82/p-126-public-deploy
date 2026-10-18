"""Remove conversation payloads from known migration backups, retaining effect ledgers."""

import sqlite3
from pathlib import Path


def clear_backups(history, ledger, graphs, session_ids, profile_id):
    paths = set(history.path.parent.glob(history.path.stem + "-before-trip-plan-*.sqlite3"))
    if ledger:
        paths.update((ledger.path.parent / "backups").rglob("*.sqlite3"))
    thread_ids = set()
    for graph in graphs:
        paths.add(Path(str(graph.saver.path) + ".before-navigation-workspace.sqlite3"))
        thread_ids.update(graph.config({"session_id": sid})["configurable"]["thread_id"] for sid in session_ids)
    names = [sid if profile_id == "default" else f"profile:{profile_id}:{sid}" for sid in session_ids]
    for path in paths:
        if not path.is_file():
            continue
        with sqlite3.connect(path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "handbook_turns" in tables:
                db.executemany("DELETE FROM handbook_turns WHERE session_id=?", [(name,) for name in names])
            for table in ("checkpoints", "writes"):
                if table in tables:
                    db.executemany(f"DELETE FROM {table} WHERE thread_id=?", [(key,) for key in thread_ids])
            for sid in session_ids:
                key = f"{profile_id}:{sid}"
                if "turn_ledger" in tables:
                    db.execute("UPDATE turn_ledger SET response=? WHERE session=?", ('{"history_cleared":true}', key))
                if "dialogue_sessions" in tables:
                    db.execute("DELETE FROM dialogue_sessions WHERE session=?", (key,))
                if "suspended_tasks" in tables:
                    db.execute("DELETE FROM suspended_tasks WHERE session=?", (key,))
