from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from src.vivi.config import Settings

_CHUNK_BYTES = 32 * 1024


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

    def _config_message(self) -> str:
        message = {
            "api_key": self.api_key,
            "model": self.model,
            "audio_format": "auto",
        }
        if self.language_hints:
            message["language_hints"] = self.language_hints
        return json.dumps(message)

    async def transcribe(self, path: Path) -> str:
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        audio = await asyncio.to_thread(Path(path).read_bytes)

        async def chunks() -> AsyncIterator[bytes]:
            for start in range(0, len(audio), _CHUNK_BYTES):
                yield audio[start : start + _CHUNK_BYTES]

        return await asyncio.wait_for(self.stream(chunks()), timeout=self.timeout)

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        on_update: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> str:
        """Relay audio chunks to Soniox while they arrive.

        ``on_update(final, interim)`` receives the confirmed text so far and the
        still-changing tail, so a UI can show words while the user speaks.
        """
        available, reason = self.availability()
        if not available:
            raise RuntimeError(reason)
        import websockets

        async with websockets.connect(self.url, max_size=None) as socket:
            await socket.send(self._config_message())

            async def pump() -> None:
                try:
                    async for chunk in audio_chunks:
                        if chunk:
                            await socket.send(chunk)
                finally:
                    # An empty *text* frame ends the audio, also when the source
                    # disconnects early. An empty binary frame is ignored by
                    # Soniox and the session hangs until a 408 timeout.
                    await socket.send("")

            sender = asyncio.create_task(pump())
            final_tokens: list[str] = []
            try:
                async for raw in socket:
                    response = json.loads(raw)
                    if response.get("error_code"):
                        raise RuntimeError(
                            f"Soniox {response['error_code']}: {response.get('error_message', '')}"
                        )
                    interim_tokens: list[str] = []
                    for token in response.get("tokens", []):
                        text = token.get("text", "")
                        # Skip control markers such as <end> and <fin>.
                        if not text or text.startswith("<"):
                            continue
                        (final_tokens if token.get("is_final") else interim_tokens).append(text)
                    if on_update is not None and response.get("tokens"):
                        await on_update("".join(final_tokens), "".join(interim_tokens))
                    if response.get("finished"):
                        break
                await sender
            finally:
                sender.cancel()
        return "".join(final_tokens).strip()
