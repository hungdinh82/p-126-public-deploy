from __future__ import annotations

import asyncio
import io
import json
import re
import unicodedata
import wave
import zipfile
from pathlib import Path

from server.config import Settings


class ZeroTTSAdapter:
    name = "zerotts"

    def __init__(self, config: Settings):
        self.config = config
        self._model = None
        self._voice = None
        self._lock = asyncio.Lock()

    def availability(self) -> tuple[bool, str]:
        try:
            import numpy  # noqa: F401
            import zerotts  # noqa: F401
        except ImportError:
            return False, "Cài requirements-ai.txt để dùng ZeroTTS"
        loaded = self._model is not None and self._voice is not None
        return loaded, "loaded" if loaded else "not-loaded"

    async def preload(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load)
            if self._voice is None:
                raise RuntimeError("Giọng TTS chưa được nạp")

    def _load(self):
        if self._model is None:
            from zerotts import ZeroTTS
            model = ZeroTTS.from_pretrained(self.config.zerotts_model)
            if self.config.zerotts_voice == "VIVI":
                import numpy as np
                from zerotts.voices import Voice

                pack = Path(__file__).resolve().parents[2] / "voices" / "VIVI.zip"
                with zipfile.ZipFile(pack) as archive:
                    meta = json.loads(archive.read("VIVI/meta.json"))
                    if meta.get("name") != "VIVI":
                        raise RuntimeError("Voice pack VIVI có ID không hợp lệ")
                    with np.load(io.BytesIO(archive.read("VIVI/voice.npz"))) as data:
                        voice = data["voice_emb"]
                        queries = int(data["n_voice_queries"])
                if voice.shape != (1, model.n_voice_queries, model.d_model) or queries != model.n_voice_queries:
                    raise RuntimeError("Voice pack VIVI không tương thích với model ZeroTTS")
                self._voice = Voice(name="VIVI", emb=voice.astype(np.float32), meta=meta)
                self._model = model
                return self._model
            requested = self._normalize_voice_name(self.config.zerotts_voice)
            voices = model.list_voices()
            matches = {self._normalize_voice_name(voice): voice for voice in voices}
            if requested not in matches:
                available = ", ".join(voices) or "không có"
                raise RuntimeError(
                    f"Không tìm thấy giọng '{self.config.zerotts_voice}'. "
                    f"Các giọng hiện có: {available}"
                )
            self._voice = matches[requested]
            self._model = model
        return self._model

    @staticmethod
    def _normalize_voice_name(value: str) -> str:
        normalized = unicodedata.normalize("NFD", value.casefold())
        without_accents = "".join(
            character for character in normalized
            if unicodedata.category(character) != "Mn"
        )
        return re.sub(r"[^a-z0-9]", "", without_accents)

    def _synthesize(self, text: str) -> bytes:
        import numpy as np
        model = self._load()
        audio = np.asarray(model.synthesize(text, voice=self._voice), dtype=np.float32).reshape(-1)
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(model.sample_rate))
            wav.writeframes(pcm)
        return output.getvalue()

    async def synthesize(self, text: str) -> bytes:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        async with self._lock:
            return await asyncio.to_thread(self._synthesize, text)

    async def stream(self, text: str):
        """Yield little-endian signed 16-bit mono PCM while ZeroTTS generates it."""
        import numpy as np

        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        async with self._lock:
            chunks = self._model.synthesize_stream(text, voice=self._voice)

            def next_chunk():
                try:
                    return next(chunks)
                except StopIteration:
                    return None

            try:
                while True:
                    next_task = asyncio.create_task(asyncio.to_thread(next_chunk))
                    try:
                        chunk = await asyncio.shield(next_task)
                    except asyncio.CancelledError:
                        # Keep the model lock until the in-flight inference step ends.
                        await next_task
                        raise
                    if chunk is None:
                        break
                    pcm = (np.clip(np.asarray(chunk, dtype=np.float32).reshape(-1), -1, 1) * 32767).astype("<i2")
                    if pcm.size:
                        yield pcm.tobytes()
            finally:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()
