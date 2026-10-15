"""STT through the remote inference gateway; audio conversion stays on Laptop."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import httpx

from src.vivi.config import Settings


class JetsonSTTAdapter:
    name = "jetson"
    device = "remote"
    dtype = "server-managed"
    streaming = False

    def __init__(self, config: Settings):
        self.config = config
        self._availability_cache = None
        self._availability_until = 0.

    def _headers(self):
        return {"Authorization": f"Bearer {self.config.jetson_ai_token}"} if self.config.ai_server_auth_required else {}

    def availability(self) -> tuple[bool, str]:
        if not self.config.jetson_ai_url:
            return False, "Thiếu AI_SERVER_URL cho STT Jetson"
        if self.config.ai_server_auth_required and not self.config.jetson_ai_token:
            return False, "Thiếu AI_SERVER_TOKEN cho STT Jetson"
        if shutil.which(self.config.ffmpeg_binary) is None:
            return False, "Thiếu ffmpeg để chuyển audio thành WAV"
        if self._availability_cache is not None and time.monotonic() < self._availability_until:
            return self._availability_cache
        try:
            with httpx.Client(timeout=2, trust_env=False) as client:
                reply = client.get(self.config.jetson_ai_url.rstrip('/') + '/v1/capabilities', headers=self._headers())
                reply.raise_for_status()
                state = reply.json()['capabilities']['stt']
            result = state['status'] in {'ready', 'busy'}, state.get('error') or state['status']
            self._availability_cache, self._availability_until = result, time.monotonic() + 1
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return False, f"STT Jetson chưa kết nối: {exc}"

    async def preload(self) -> None:
        available, reason = await asyncio.to_thread(self.availability)
        if not available:
            raise RuntimeError(reason)

    def _wav(self, path: Path) -> bytes:
        with tempfile.TemporaryDirectory(prefix='vivi-jetson-stt-') as directory:
            target = Path(directory) / 'input.wav'
            result = subprocess.run([self.config.ffmpeg_binary, '-nostdin', '-loglevel', 'error', '-y',
                                     '-i', str(path), '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(target)],
                                    capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError('Không chuyển được audio: ' + result.stderr.decode(errors='replace'))
            with wave.open(str(target), 'rb') as wav:
                too_long = wav.getnframes() / wav.getframerate() > 60
            if too_long or target.stat().st_size > 2_000_000:
                raise ValueError('Audio vượt giới hạn 60 giây của gateway Jetson')
            return target.read_bytes()

    async def transcribe(self, path: Path) -> str:
        audio = await asyncio.to_thread(self._wav, path)
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(self.config.jetson_ai_url.rstrip('/') + '/v1/stt/transcriptions',
                                         headers=self._headers(), files={'file': ('input.wav', audio, 'audio/wav')})
            response.raise_for_status()
            text = response.json()['text']
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError('STT Jetson trả về transcript rỗng')
        return text.strip()
