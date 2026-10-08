from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from src.vivi.config import Settings

_CHUNK_BYTES = 32 * 1024
_ENDPOINT = "<end>"
# Background closes of sessions abandoned at an endpoint (keeps tasks alive).
_closing: set[asyncio.Task] = set()
# Domain vocabulary so short in-car commands are not misheard.
_CONTEXT = {
    "general": [
        {"key": "domain", "value": "Trợ lý giọng nói trong xe ô tô điện"},
        {"key": "topic", "value": "Điều khiển xe VinFast VF8 bằng tiếng Việt"},
    ],
    "terms": [
        "ViVi", "VinFast", "VF8", "nắp capo", "cốp sau", "cửa sổ", "cửa kính",
        "bên tài", "bên phụ", "ghế sau", "sưởi ghế", "điều hoà", "khoá cửa",
        "mở khoá", "áp suất lốp", "quãng đường", "phần trăm pin", "chỉ đường",
    ],
}


class SonioxAdapter:
    """Cloud STT through the Soniox real-time WebSocket API.

    ``stream`` relays live microphone chunks; ``transcribe`` replays an
    uploaded file through the same session. ``audio_format=auto`` lets Soniox
    decode webm/ogg/wav/mp3 without a local ffmpeg.
    """

    name = "soniox"
    device = "cloud"
    dtype = "cloud"

    def __init__(self, config: Settings) -> None:
        self.api_key = config.soniox_api_key
        self.model = config.soniox_model
        self.url = config.soniox_ws_url
        self.language_hints = [
            code.strip() for code in config.soniox_language_hints.split(",") if code.strip()
        ]
        self.language_hints_strict = config.soniox_language_hints_strict
        self.endpoint_detection = config.soniox_endpoint_detection
        self.max_endpoint_delay_ms = config.soniox_max_endpoint_delay_ms
        self.timeout = config.soniox_timeout_seconds
        self.stream_max_seconds = config.soniox_stream_max_seconds

    def availability(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "Thiếu SONIOX_API_KEY"
        try:
            import websockets  # noqa: F401
        except ImportError:
            return False, "Cài gói websockets để dùng Soniox"
        return True, f"ready ({self.model})"

    async def preload(self) -> None:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)

    def _config_message(self, endpoint_detection: bool = False) -> str:
        message = {
            "api_key": self.api_key,
            "model": self.model,
            "audio_format": "auto",
            "context": _CONTEXT,
        }
        if self.language_hints:
            message["language_hints"] = self.language_hints
            message["language_hints_strict"] = self.language_hints_strict
        if endpoint_detection:
            message["enable_endpoint_detection"] = True
            message["max_endpoint_delay_ms"] = self.max_endpoint_delay_ms
        return json.dumps(message, ensure_ascii=False)

    async def transcribe(self, path: Path) -> str:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        audio = await asyncio.to_thread(Path(path).read_bytes)

        async def chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(audio), _CHUNK_BYTES):
                yield audio[start : start + _CHUNK_BYTES]

        return await asyncio.wait_for(self.stream(chunks(), live=False), timeout=self.timeout)

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        on_update: Callable[[str, str], Awaitable[None]] | None = None,
        live: bool = True,
    ) -> str:
        """Relay audio chunks to Soniox while they arrive.

        ``on_update(final, interim)`` receives the confirmed text so far and the
        still-changing tail, so a UI can show words while the user speaks.
        A live session returns as soon as Soniox detects the end of speech; an
        uploaded file (``live=False``) is read to the end, pauses included.
        """
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        import websockets

        socket = await websockets.connect(self.url, max_size=None)
        stop_at_endpoint = live and self.endpoint_detection
        at_endpoint = False
        final_tokens: list[str] = []

        async def pump() -> None:
            try:
                async for chunk in audio_chunks:
                    if chunk:
                        await socket.send(chunk)
            finally:
                # An empty *text* frame ends the audio, also when the source
                # disconnects early. An empty binary frame is ignored by
                # Soniox and the session hangs until a 408 timeout.
                if not at_endpoint:
                    await socket.send("")

        sender = None
        try:
            await socket.send(self._config_message(stop_at_endpoint))
            sender = asyncio.create_task(pump())
            async for raw in socket:
                response = json.loads(raw)
                if response.get("error_code"):
                    raise RuntimeError(
                        f"Soniox {response['error_code']}: {response.get('error_message', '')}"
                    )
                interim_tokens: list[str] = []
                endpoint = False
                for token in response.get("tokens", []):
                    text = token.get("text", "")
                    # Skip control markers such as <end> and <fin>.
                    if not text or text.startswith("<"):
                        endpoint = endpoint or text == _ENDPOINT
                        continue
                    (final_tokens if token.get("is_final") else interim_tokens).append(text)
                if on_update is not None and response.get("tokens"):
                    await on_update("".join(final_tokens), "".join(interim_tokens))
                if response.get("finished"):
                    break
                # Every token before <end> is final, so the utterance is complete.
                if stop_at_endpoint and endpoint and "".join(final_tokens).strip():
                    at_endpoint = True
                    break
            else:
                await sender
        finally:
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if at_endpoint:
                # Soniox takes ~1 s to acknowledge a close mid-session; the
                # transcript is already complete, so do not make the user wait.
                task = asyncio.create_task(socket.close())
                _closing.add(task)
                task.add_done_callback(_closing.discard)
            else:
                await socket.close()
        return "".join(final_tokens).strip()
