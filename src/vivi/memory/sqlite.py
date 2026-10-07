from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.vivi.text import normalize_text

PROFILE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
LABELS = {
    "display_name": "Tên bạn",
    "preferred_temperature": "Nhiệt độ bạn thích",
    "preferred_music": "Nhạc bạn thích",
    "response_style": "Cách trả lời bạn muốn",
}


class MemoryValue(BaseModel):
    """Typed preferences plus plain notes/command transcripts, never executable code."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_.-]+$")
    kind: Literal["fact", "preference", "note", "command"]
    value: str | float = Field(union_mode="left_to_right")
    label: str = Field(default="Ghi chú", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_value(self):
        if self.key == "preferred_temperature":
            try:
                temperature = float(self.value)
            except (ValueError, TypeError) as exc:
                raise ValueError("Nhiệt độ phải là một số từ 16 đến 30 độ C.") from exc
            if not 16 <= temperature <= 30:
                raise ValueError("Nhiệt độ hỗ trợ nằm trong khoảng 16 đến 30 độ C.")
            self.value, self.kind = temperature, "preference"
        elif self.key in LABELS:
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 120:
                raise ValueError("Thông tin cần ghi nhớ phải là văn bản ngắn, tối đa 120 ký tự.")
            self.value = self.value.strip()
            self.kind = "fact" if self.key == "display_name" else "preference"
            if self.key == "response_style" and self.value not in {"ngắn gọn", "chi tiết"}:
                raise ValueError("Cách trả lời phải là ngắn gọn hoặc chi tiết.")
        else:
            expected = "command" if self.key.startswith("command.") else "note"
            if not self.key.startswith(("note.", "command.")) or self.kind != expected:
                raise ValueError("Chỉ hỗ trợ các sở thích đã định nghĩa, note.* hoặc command.*.")
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 700:
                raise ValueError("Ghi chú hoặc lệnh cần có nội dung, tối đa 700 ký tự.")
            self.value = self.value.strip()
        if self.key in LABELS:
            self.label = LABELS[self.key]
        return self


class SQLiteLongTermMemory:
    """Short connections, atomic replacement, bounded profiles; no embedding/model I/O."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect():
            pass

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS memory_items (
                        profile_id TEXT NOT NULL, key TEXT NOT NULL,
                        kind TEXT NOT NULL, value_json TEXT NOT NULL, label TEXT NOT NULL,
                        source_session_id TEXT NOT NULL, source_turn_id TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY(profile_id, key)
                    )
                """)
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _profile(profile_id: str):
        if not PROFILE_RE.fullmatch(profile_id):
            raise ValueError("Invalid memory profile ID")

    @staticmethod
    def _item(row):
        result = dict(row)
        result["value"] = json.loads(result.pop("value_json"))
        return result

    def list(self, profile_id: str) -> list[dict]:
        self._profile(profile_id)
        with self._connect() as db:
            return [self._item(row) for row in db.execute(
                "SELECT * FROM memory_items WHERE profile_id=? ORDER BY updated_at DESC, key", (profile_id,)
            )]

    def remember(self, profile_id: str, item: MemoryValue, *, session_id="api", turn_id="api") -> dict:
        self._profile(profile_id)
        now = datetime.now(UTC).isoformat()
        encoded = json.dumps(item.value, ensure_ascii=False, allow_nan=False)
        with self._connect() as db:
            # Serialize the quota check and upsert across workers/processes.
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM memory_items WHERE profile_id=? AND key=?",
                                  (profile_id, item.key)).fetchone()
            if previous and previous["value_json"] == encoded and previous["label"] == item.label:
                return self._item(previous)
            count = db.execute("SELECT COUNT(*) FROM memory_items WHERE profile_id=?", (profile_id,)).fetchone()[0]
            if previous is None and count >= 200:
                raise ValueError("Bộ nhớ đã đầy. Bạn hãy xoá bớt một ghi chú trước nhé.")
            db.execute("""
                INSERT INTO memory_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(profile_id, key) DO UPDATE SET
                    kind=excluded.kind, value_json=excluded.value_json, label=excluded.label,
                    source_session_id=excluded.source_session_id, source_turn_id=excluded.source_turn_id,
                    updated_at=excluded.updated_at, revision=memory_items.revision+1
            """, (profile_id, item.key, item.kind, encoded, item.label, session_id, turn_id, now, now))
            return self._item(db.execute("SELECT * FROM memory_items WHERE profile_id=? AND key=?",
                                         (profile_id, item.key)).fetchone())

    def forget(self, profile_id: str, key: str) -> bool:
        self._profile(profile_id)
        with self._connect() as db:
            return db.execute("DELETE FROM memory_items WHERE profile_id=? AND key=?", (profile_id, key)).rowcount > 0

    def reset(self, profile_id: str) -> int:
        self._profile(profile_id)
        with self._connect() as db:
            return db.execute("DELETE FROM memory_items WHERE profile_id=?", (profile_id,)).rowcount

    def context(self, profile_id: str, query: str, *, limit=8, max_characters=2000) -> list[dict]:
        items = self.list(profile_id)
        words = set(re.findall(r"\w+", normalize_text(query))) - {"toi", "ban", "minh", "la", "va", "cua", "gi"}

        def score(item):
            tokens = set(re.findall(r"\w+", normalize_text(f"{item['label']} {item['value']}")))
            return (item["key"] in LABELS, len(tokens & words))

        # Preferences are small and always useful; notes require lexical relevance.
        candidates = [item for item in items if item["key"] in LABELS or score(item)[1] > 0]
        candidates.sort(key=score, reverse=True)
        result, used = [], 2
        for item in candidates:
            compact = {key: item[key] for key in ("key", "kind", "label", "value")}
            size = len(json.dumps(compact, ensure_ascii=False)) + 2
            if used + size > max_characters:
                continue
            result.append(compact)
            used += size
            if len(result) >= limit:
                break
        return result
