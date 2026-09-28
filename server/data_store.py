from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.vivi.config import Settings


class DataStore:
    def __init__(self, config: Settings):
        self.config = config
        self._lock = threading.Lock()

    def _day_dir(self) -> Path:
        path = self.config.data_dir / datetime.now(UTC).strftime("%Y-%m-%d")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _safe(value: str) -> str:
        cleaned = "".join(c for c in value if c.isalnum() or c in "-_")
        return cleaned[:100] or "unknown"

    def save_audio(self, session_id: str, turn_id: str, suffix: str, content: bytes) -> Path | None:
        if not self.config.store_audio:
            return None
        ext = suffix.lower() if suffix.lower() in {".wav", ".webm", ".ogg", ".mp3", ".m4a"} else ".bin"
        path = self._day_dir() / f"{self._safe(session_id)}_{self._safe(turn_id)}{ext}"
        path.write_bytes(content)
        return path

    def append_event(self, event: dict[str, Any]) -> None:
        if not self.config.store_transcripts:
            return
        payload = {"timestamp": datetime.now(UTC).isoformat(), **event}
        path = self._day_dir() / "turns.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
