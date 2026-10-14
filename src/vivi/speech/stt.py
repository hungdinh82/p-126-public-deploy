from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
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


class JetsonSTTAdapter:
    """Batch STT adapter for the private Jetson inference gateway."""

    name = "jetson-whisper"
    device = "jetson"
    dtype = "remote"

    def __init__(self, config: Settings):
        self.config = config
        self.transport = None

    def availability(self) -> tuple[bool, str]:
        if not self.config.jetson_ai_url:
            return False, "AI_SERVER_URL chưa cấu hình"
        if self.config.ai_server_auth_required and not self.config.jetson_ai_token:
            return False, "Thiếu AI_SERVER_TOKEN"
        if shutil.which(self.config.ffmpeg_binary) is None:
            return False, "Không tìm thấy ffmpeg để chuẩn hóa audio cho Jetson"
        return True, "ready-via-gateway"

    async def preload(self) -> None:
        return None

    async def transcribe(self, path: Path) -> str:
        import httpx

        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        with tempfile.NamedTemporaryFile(prefix="vivi-jetson-", suffix=".wav", delete=False) as handle:
            wav_path = Path(handle.name)
        try:
            process = await asyncio.create_subprocess_exec(
                self.config.ffmpeg_binary, "-v", "error", "-y", "-i", str(path),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, error = await process.communicate()
            if process.returncode:
                raise RuntimeError(f"Không chuyển được audio cho Jetson: {error.decode('utf-8', 'replace')[:160]}")
            headers = (
                {"Authorization": f"Bearer {self.config.jetson_ai_token}"}
                if self.config.ai_server_auth_required else {}
            )
            async with httpx.AsyncClient(timeout=self.config.soniox_timeout_seconds, transport=self.transport) as client:
                with wav_path.open("rb") as audio:
                    response = await client.post(
                        self.config.jetson_ai_url.rstrip("/") + "/v1/stt/transcriptions",
                        headers=headers, files={"file": ("audio.wav", audio, "audio/wav")},
                    )
            response.raise_for_status()
            return str(response.json().get("text", "")).strip()
        finally:
            wav_path.unlink(missing_ok=True)


class AdaptiveSTTAdapter(FallbackSTTAdapter):
    def __init__(self, primary, fallback, connectivity):
        super().__init__(primary, fallback)
        self.connectivity = connectivity

    def _active(self):
        if self.connectivity.capability_available("stt", "cloud") and self.primary.availability()[0]:
            return self.primary
        return self.fallback

    @property
    def streaming(self) -> bool:
        return self._active() is self.primary and super().streaming

    def availability(self) -> tuple[bool, str]:
        if self.connectivity.capability_available("stt", "cloud") and self.primary.availability()[0]:
            return self.primary.availability()
        if self.connectivity.capability_available("stt", "jetson"):
            return self.fallback.availability()
        return False, "STT không khả dụng theo trạng thái kết nối/profile hiện tại"

    async def transcribe(self, path: Path) -> str:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        if self._active() is self.primary:
            try:
                return await self.primary.transcribe(path)
            except Exception:
                logger.exception("%s lỗi, chuyển sang %s", self.primary.name, self.fallback.name)
        return await self.fallback.transcribe(path)


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


def create_stt(config: Settings, connectivity=None):
    if config.jetson_ai_policy == "offline_only" and config.stt_provider == "soniox":
        # The configured on-device fallback becomes the primary in offline mode.
        return _create_single(config.stt_fallback_provider, config)
    primary = _create_single(config.stt_provider, config)
    fallback = config.stt_fallback_provider
    if config.stt_provider in {"off", fallback} or fallback == "off":
        return primary
    fallback_adapter = _create_single(fallback, config)
    if connectivity is not None and config.jetson_ai_url and config.jetson_ai_token and config.jetson_ai_policy != "disabled":
        fallback_adapter = JetsonSTTAdapter(config)
    if connectivity is not None:
        return AdaptiveSTTAdapter(primary, fallback_adapter, connectivity)
    return FallbackSTTAdapter(primary, fallback_adapter)
