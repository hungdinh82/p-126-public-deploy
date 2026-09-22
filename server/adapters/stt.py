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

    def availability(self) -> tuple[bool, str]:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
            return True, "ready-to-load"
        except ImportError:
            return False, "Cài requirements-ai.txt để dùng PhoWhisper"

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        import torch
        from transformers import pipeline
        requested = self.config.phowhisper_device
        if requested == "auto":
            device = "cuda:0" if torch.cuda.is_available() else "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
        else:
            device = requested
        dtype = torch.float16 if str(device).startswith("cuda") else torch.float32
        self._pipeline = pipeline("automatic-speech-recognition", model=self.config.phowhisper_model, device=device, torch_dtype=dtype)
        return self._pipeline

    async def transcribe(self, path: Path) -> str:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        async with self._lock:
            result = await asyncio.to_thread(self._load().__call__, str(path))
        return str(result.get("text", "")).strip()

