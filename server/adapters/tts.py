from __future__ import annotations

import asyncio
import io
import re
import unicodedata
import wave

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
            return True, "ready-to-load"
        except ImportError:
            return False, "Cài requirements-ai.txt để dùng ZeroTTS"

    def _load(self):
        if self._model is None:
            from zerotts import ZeroTTS
            self._model = ZeroTTS.from_pretrained(self.config.zerotts_model)
            requested = self._normalize_voice_name(self.config.zerotts_voice)
            voices = self._model.list_voices()
            matches = {self._normalize_voice_name(voice): voice for voice in voices}
            if requested not in matches:
                available = ", ".join(voices) or "không có"
                raise RuntimeError(
                    f"Không tìm thấy giọng '{self.config.zerotts_voice}'. "
                    f"Các giọng hiện có: {available}"
                )
            self._voice = matches[requested]
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
