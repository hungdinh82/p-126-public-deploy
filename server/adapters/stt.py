from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from src.vivi.config import Settings


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


def create_stt(config: Settings):
    if config.stt_provider == "off":
        return DisabledSTTAdapter()
    if config.stt_provider == "phowhisper":
        return PhoWhisperAdapter(config)
    if config.stt_provider == "whisper_cpp":
        from server.adapters.whisper_cpp import WhisperCppAdapter

        return WhisperCppAdapter(config)
    raise ValueError(f"STT_PROVIDER không hợp lệ: {config.stt_provider}")
