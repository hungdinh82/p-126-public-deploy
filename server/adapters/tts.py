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


class DisabledTTSAdapter:
    name = "off"
    device = "disabled"
    execution_providers: list[str] = []

    @staticmethod
    def availability() -> tuple[bool, str]:
        return False, "TTS đã tắt bằng TTS_PROVIDER=off; response text vẫn hoạt động"

    async def preload(self) -> None:
        return None

    async def synthesize(self, text: str) -> bytes:
        del text
        raise RuntimeError("TTS đang tắt; hãy đổi TTS_PROVIDER")

    async def stream(self, text: str):
        del text
        if False:
            yield b""
        raise RuntimeError("TTS đang tắt; hãy đổi TTS_PROVIDER")


class ZeroTTSAdapter:
    name = "zerotts"

    def __init__(self, config: Settings):
        self.config = config
        self._model = None
        self._voice = None
        self._execution_providers: tuple[str, ...] = ()
        self._lock = asyncio.Lock()

    @property
    def device(self) -> str:
        if "CUDAExecutionProvider" in self._execution_providers:
            return "cuda"
        if "CPUExecutionProvider" in self._execution_providers:
            return "cpu"
        return "not-loaded"

    @property
    def execution_providers(self) -> list[str]:
        return list(self._execution_providers)

    @staticmethod
    def _select_execution_providers(device: str, available: list[str]) -> list[str]:
        requested = device.strip().lower()
        if requested not in {"auto", "cpu", "cuda"}:
            raise RuntimeError("ZEROTTS_DEVICE phải là auto, cpu hoặc cuda")
        if requested == "cpu":
            return ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if requested == "cuda":
            raise RuntimeError(
                "ZEROTTS_DEVICE=cuda nhưng ONNX Runtime không có CUDAExecutionProvider"
            )
        return ["CPUExecutionProvider"]

    def _providers(self) -> list[str]:
        import onnxruntime as ort

        requested = self.config.zerotts_device.strip().lower()
        if requested in {"auto", "cuda"}:
            # Load the pinned CUDA/cuDNN wheels from NVIDIA site-packages. An
            # empty directory skips an incompatible CPU-only PyTorch install.
            ort.preload_dlls(directory="")
        return self._select_execution_providers(requested, ort.get_available_providers())

    def availability(self) -> tuple[bool, str]:
        if self._model is not None and self._voice is not None:
            return True, "loaded"
        try:
            import numpy  # noqa: F401
            import zerotts  # noqa: F401
        except ImportError:
            return False, "Cài requirements-ai.txt để dùng ZeroTTS"
        return True, "ready-to-load"

    async def ensure_loaded(self) -> None:
        if self._model is None or self._voice is None:
            await self.preload()

    async def preload(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._load)
            if self._voice is None:
                raise RuntimeError("Giọng TTS chưa được nạp")

    def _load(self):
        if self._model is None:
            from zerotts import ZeroTTS
            providers = self._providers()
            model = ZeroTTS.from_pretrained(self.config.zerotts_model, providers=providers)
            actual = tuple(model.prefix_step_sess.get_providers())
            if providers[0] == "CUDAExecutionProvider" and (
                not actual or actual[0] != "CUDAExecutionProvider"
            ):
                raise RuntimeError(
                    "ZeroTTS không tạo được CUDA session; kiểm tra CUDA/cuDNN runtime"
                )
            self._execution_providers = actual
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
            model = await asyncio.to_thread(self._load)
            chunks = model.synthesize_stream(text, voice=self._voice)

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


def create_tts(config: Settings):
    if config.tts_provider == "off":
        return DisabledTTSAdapter()
    if config.tts_provider == "zerotts":
        return ZeroTTSAdapter(config)
    raise ValueError(f"TTS_PROVIDER không hợp lệ: {config.tts_provider}")
