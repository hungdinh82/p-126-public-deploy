from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from src.vivi.config import Settings

logger = logging.getLogger(__name__)


class DisabledSTTAdapter:
    name = "off"
    device = "disabled"
    dtype = "disabled"

    @staticmethod
    def availability() -> tuple[bool, str]:
        return False, "STT đã tắt bằng STT_PROVIDER=off; text input vẫn hoạt động"

    async def preload(self) -> None:
        return None

    async def transcribe(self, path: Path) -> str:
        del path
        raise RuntimeError("STT đang tắt; hãy dùng text input hoặc đổi STT_PROVIDER")


class PhoWhisperAdapter:
    name = "phowhisper"

    def __init__(self, config: Settings):
        self.config = config
        self._pipeline = None
        self._lock = asyncio.Lock()
        self.device = "not-loaded"
        self.dtype = "not-loaded"

    def availability(self) -> tuple[bool, str]:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError:
            return False, "Cài requirements-ai.txt để dùng PhoWhisper"
        if shutil.which(self.config.ffmpeg_binary) is None:
            return False, (
                f"Không tìm thấy ffmpeg trong PATH (FFMPEG_BINARY={self.config.ffmpeg_binary!r}). "
                "Ubuntu/Debian: sudo apt install ffmpeg; macOS: brew install ffmpeg"
            )
        return True, "loaded" if self._pipeline is not None else "ready-to-load"

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        import torch
        from transformers import pipeline
        requested = self.config.phowhisper_device.lower()
        if requested == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
        else:
            device = requested
        requested_dtype = self.config.phowhisper_dtype.lower()
        if requested_dtype == "auto":
            # FP16 is materially faster for Whisper on both CUDA and Apple MPS.
            dtype = torch.float16 if str(device).startswith(("cuda", "mps")) else torch.float32
        else:
            dtype = {"float16": torch.float16, "float32": torch.float32}.get(requested_dtype)
            if dtype is None:
                raise RuntimeError(f"PHOWHISPER_DTYPE không hợp lệ: {self.config.phowhisper_dtype}")
        self._pipeline = pipeline(
            "automatic-speech-recognition",
            model=self.config.phowhisper_model,
            device=device,
            dtype=dtype,
        )
        self.device = str(device)
        self.dtype = str(dtype).removeprefix("torch.")
        return self._pipeline

    async def preload(self) -> None:
        """Load and warm the model before the first microphone turn."""
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        import numpy as np

        async with self._lock:
            model = await asyncio.to_thread(self._load)
            silent_audio = {"array": np.zeros(1600, dtype=np.float32), "sampling_rate": 16000}
            await asyncio.to_thread(
                model.__call__,
                silent_audio,
                generate_kwargs={"language": self.config.phowhisper_language, "task": "transcribe", "num_beams": 1},
            )

    async def transcribe(self, path: Path) -> str:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        async with self._lock:
            result = await asyncio.to_thread(
                self._load().__call__,
                str(path),
                generate_kwargs={"language": self.config.phowhisper_language, "task": "transcribe", "num_beams": 1},
            )
        return str(result.get("text", "")).strip()


class FallbackSTTAdapter:
    """Use the primary STT (e.g. Soniox) and fall back to a local model.

    The fallback answers when the primary is unavailable (missing key) or a
    request fails, so the microphone keeps working offline.
    """

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def _active(self):
        return self.primary if self.primary.availability()[0] else self.fallback

    @property
    def name(self) -> str:
        active = self._active()
        return active.name if active is self.primary else f"{active.name} (dự phòng)"

    @property
    def device(self) -> str:
        return self._active().device

    @property
    def dtype(self) -> str:
        return self._active().dtype

    @property
    def streaming(self) -> bool:
        return hasattr(self.primary, "stream") and self.primary.availability()[0]

    @property
    def stream_max_seconds(self) -> float:
        return getattr(self.primary, "stream_max_seconds", 60)

    def availability(self) -> tuple[bool, str]:
        primary_ok, primary_detail = self.primary.availability()
        if primary_ok:
            return True, primary_detail
        fallback_ok, fallback_detail = self.fallback.availability()
        return fallback_ok, f"{self.primary.name}: {primary_detail}; dùng {self.fallback.name}: {fallback_detail}"

    async def preload(self) -> None:
        await self.primary.preload()

    async def preload_fallback(self) -> None:
        if self.fallback.availability()[0]:
            await self.fallback.preload()

    async def transcribe(self, path: Path) -> str:
        if self.primary.availability()[0]:
            try:
                return await self.primary.transcribe(path)
            except Exception:
                logger.exception("%s lỗi, chuyển sang %s", self.primary.name, self.fallback.name)
        return await self.fallback.transcribe(path)

    async def stream(self, audio_chunks, on_update=None) -> str:
        return await self.primary.stream(audio_chunks, on_update)


def _create_single(provider: str, config: Settings):
    if provider == "off":
        return DisabledSTTAdapter()
    if provider == "phowhisper":
        return PhoWhisperAdapter(config)
    if provider == "whisper_cpp":
        from src.vivi.speech.whisper_cpp import WhisperCppAdapter

        return WhisperCppAdapter(config)
    if provider == "soniox":
        from src.vivi.speech.soniox import SonioxAdapter

        return SonioxAdapter(config)
    raise ValueError(f"STT_PROVIDER không hợp lệ: {provider}")


def create_stt(config: Settings):
    primary = _create_single(config.stt_provider, config)
    fallback = config.stt_fallback_provider
    if config.stt_provider in {"off", fallback} or fallback == "off":
        return primary
    return FallbackSTTAdapter(primary, _create_single(fallback, config))
