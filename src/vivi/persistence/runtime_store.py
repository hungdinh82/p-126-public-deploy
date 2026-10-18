"""Request deduplication and execution reconciliation.

SQLite transactions own reservations. Checkpoints are workflow state; this ledger
records external effects and never assumes that a crashed dispatch did not occur.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class RuntimeStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS dialogue_sessions (
                    session TEXT PRIMARY KEY, task TEXT, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS turn_ledger (
                    session TEXT, turn TEXT, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    response TEXT, PRIMARY KEY(session,turn));
                CREATE TABLE IF NOT EXISTS command_ledger (
                    session TEXT, turn TEXT, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, PRIMARY KEY(session,turn));
                CREATE TABLE IF NOT EXISTS command_turns (session TEXT, turn TEXT, command TEXT, PRIMARY KEY(session,turn,command));
                CREATE TABLE IF NOT EXISTS runtime_schema (version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS suspended_tasks (session TEXT, id TEXT, payload TEXT,
                    PRIMARY KEY(session,id));
                INSERT OR IGNORE INTO runtime_schema VALUES (1);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def reserve_turn(self, session: str, turn: str, payload: dict) -> dict | None:
        key = fingerprint(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turn_ledger WHERE session=? AND turn=?", (session, turn)).fetchone()
            if row:
                if row["fingerprint"] != key:
                    raise ValueError("turn_id đã được dùng cho một yêu cầu khác.")
                if row["response"]:
                    response = json.loads(row["response"])
                    if response.get("history_cleared"):
                        raise ValueError("Lượt này thuộc lịch sử đã xoá. Hãy gửi một lượt mới.")
                    return response
                raise ValueError("Lượt này đang xử lý hoặc cần kiểm tra lại kết quả sau gián đoạn.")
            db.execute("INSERT INTO turn_ledger VALUES (?,?,?,'running',NULL)", (session, turn, key))
        return None

    def sessions(self, profile_id):
        prefix = profile_id + ":"
        with self.connect() as db:
            rows = db.execute("SELECT DISTINCT session FROM turn_ledger").fetchall()
        return [row[0][len(prefix) :] for row in rows if row[0].startswith(prefix)]

    def clear_history(self, profile_id, session_ids):
        with self.connect() as db:
            for session_id in session_ids:
                session = f"{profile_id}:{session_id}"
                db.execute("UPDATE turn_ledger SET response=? WHERE session=?", ('{"history_cleared":true}', session))
                db.execute("DELETE FROM dialogue_sessions WHERE session=?", (session,))
                db.execute("DELETE FROM suspended_tasks WHERE session=?", (session,))

    def finish_turn(self, session: str, turn: str, response: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE turn_ledger SET status='completed',response=? WHERE session=? AND turn=?",
                (json.dumps(response, ensure_ascii=False), session, turn),
            )

    def fail_turn(self, session: str, turn: str):
        with self.connect() as db:
            db.execute("UPDATE turn_ledger SET status='interrupted' WHERE session=? AND turn=?", (session, turn))

    def reserve_command(self, session: str, turn: str, action: dict) -> dict | None:
        key = fingerprint(action)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM command_ledger WHERE session=? AND turn=?", (session, turn)).fetchone()
            if row:
                if row["fingerprint"] != key:
                    raise ValueError("Command ID already belongs to another action")
                if row["result"]:
                    return json.loads(row["result"])
                raise ValueError("Command was dispatched; reconcile state before requesting another action")
            db.execute("INSERT INTO command_ledger VALUES (?,?,?,'dispatched',NULL)", (session, turn, key))
        return None

    def finish_command(self, session: str, turn: str, result: dict):
        with self.connect() as db:
            db.execute(
                "UPDATE command_ledger SET status=?,result=? WHERE session=? AND turn=?",
                ("verified" if result["verified"] else "unknown", json.dumps(result), session, turn),
            )

    @staticmethod
    def action_key(action):
        return fingerprint({"intent": action.intent, "arguments": action.arguments})

    def command_result(self, session, turn):
        with self.connect() as db:
            row = db.execute(
                "SELECT fingerprint,result FROM command_ledger WHERE session=? AND turn=?", (session, turn)
            ).fetchone()
        if row and row["result"]:
            return {"fingerprint": row["fingerprint"], "result": json.loads(row["result"])}
        return None

    def link_command(self, session, turn, command):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO command_turns VALUES (?,?,?)", (session, turn, command))

    def turn_status(self, session, turn, command_session=None):
        effect_session = command_session or session
        with self.connect() as db:
            row = db.execute(
                "SELECT status,response FROM turn_ledger WHERE session=? AND turn=?", (session, turn)
            ).fetchone()
            commands = list(
                db.execute(
                    """SELECT c.turn AS id,c.status,c.result FROM command_ledger c
                JOIN command_turns t ON t.session=c.session AND t.command=c.turn
                WHERE t.session=? AND t.turn=? ORDER BY t.rowid""",
                    (effect_session, turn),
                )
            )
            if not commands:
                commands = list(
                    db.execute(
                        "SELECT turn AS id,status,result FROM command_ledger WHERE session=? AND turn=?",
                        (effect_session, turn),
                    )
                )
        if not row:
            return None
        effects = [
            {
                "id": item["id"],
                "status": item["status"],
                "result": json.loads(item["result"]) if item["result"] else None,
            }
            for item in commands
        ]
        return {
            "status": row["status"],
            "response": json.loads(row["response"]) if row["response"] else None,
            "command": effects[-1] if effects else None,
            "commands": effects,
        }
