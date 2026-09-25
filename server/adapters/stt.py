from __future__ import annotations

import asyncio
from pathlib import Path

from server.config import Settings


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
            return True, "loaded" if self._pipeline is not None else "ready-to-load"
        except ImportError:
            return False, "Cài requirements-ai.txt để dùng PhoWhisper"

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
