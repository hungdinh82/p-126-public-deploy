from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.vivi.config import Settings


class EventStore:
    def __init__(self, config: Settings):
        self.config = config
        self._lock = threading.Lock()
        self.last_event: dict[str, Any] | None = None
        self.last_tts: dict[str, Any] | None = None
        self.recent_events: list[dict[str, Any]] = []
        self.tts_by_turn: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
        self.input_starts: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()

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
        with self._lock:
            self.last_event = event
            self.recent_events.append(event)
            del self.recent_events[:-30]
            if not self.config.store_transcripts:
                return
            payload = {"timestamp": datetime.now(UTC).isoformat(), **event}
            path = self._day_dir() / "turns.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def update_turn(self, session_id: str, turn_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            start = self.input_starts.get((session_id, turn_id))
            for event in reversed(self.recent_events):
                if event.get("session_id") == session_id and event.get("turn_id") == turn_id and event.get("event_type") == "turn":
                    event.update(values)
                    if start:
                        event["received_at"] = start[1]
                        event["input_to_output_ms"] = round((time.perf_counter() - start[0]) * 1000, 2)
                    self.last_event = event
                    break

    def mark_input_start(self, session_id: str, turn_id: str, monotonic_time: float, wall_time: float) -> None:
        with self._lock:
            self.input_starts[(session_id, turn_id)] = (monotonic_time, wall_time)
            self.input_starts.move_to_end((session_id, turn_id))
            while len(self.input_starts) > 60:
                del self.input_starts[next(iter(self.input_starts))]

    def record_tts(self, session_id: str, turn_id: str, measurement: dict[str, Any]) -> None:
        key = (session_id, turn_id)
        with self._lock:
            chunks = self.tts_by_turn.setdefault(key, [])
            self.tts_by_turn.move_to_end(key)
            chunks.append(measurement)
            del chunks[:-20]
            while len(self.tts_by_turn) > 60:
                oldest = next(iter(self.tts_by_turn))
                if oldest == key:
                    break
                del self.tts_by_turn[oldest]
            started = self.input_starts.get(key)
            if started:
                measurement["input_to_audio_ready_ms"] = round((time.perf_counter() - started[0]) * 1000, 2)
                for event in reversed(self.recent_events):
                    if event.get("event_type") == "turn" and event.get("session_id") == session_id and event.get("turn_id") == turn_id:
                        event["input_to_audio_ready_ms"] = measurement["input_to_audio_ready_ms"]
                        break
            else:
                for event in reversed(self.recent_events):
                    if event.get("event_type") == "turn" and event.get("session_id") == session_id and event.get("turn_id") == turn_id:
                        received_at = event.get("received_at")
                        if received_at is not None:
                            measurement["input_to_audio_ready_ms"] = round((time.time() - received_at) * 1000, 2)
                            event["input_to_audio_ready_ms"] = measurement["input_to_audio_ready_ms"]
                        break
            self.last_tts = measurement

    def metrics_snapshot(self) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[dict[str, Any]]], dict[str, Any] | None]:
        with self._lock:
            return (
                list(self.recent_events),
                {key: list(value) for key, value in self.tts_by_turn.items()},
                dict(self.last_tts) if self.last_tts is not None else None,
            )
