from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from server.config import Settings


class WhisperCppAdapter:
    """Low-memory STT adapter for the classic Jetson Nano profile."""

    name = "whisper.cpp"
    device = "native"
    dtype = "ggml"

    def __init__(self, config: Settings) -> None:
        self.binary = Path(config.whisper_cpp_binary)
        self.model = Path(config.whisper_cpp_model)
        self.ffmpeg = config.ffmpeg_binary
        self.language = config.phowhisper_language
        self._lock = asyncio.Lock()

    def availability(self) -> tuple[bool, str]:
        resolved = shutil.which(str(self.binary))
        binary = Path(resolved) if resolved else self.binary
        if not binary.exists() or not binary.is_file():
            return False, f"whisper.cpp binary not found: {self.binary}"
        if not self.model.exists():
            return False, f"whisper.cpp model not found: {self.model}"
        if shutil.which(self.ffmpeg) is None:
            return False, f"ffmpeg binary not found: {self.ffmpeg}"
        return True, "ready"

    async def preload(self) -> None:
        available, detail = self.availability()
        if not available:
            raise RuntimeError(detail)

    async def transcribe(self, path: Path) -> str:
        available, detail = self.availability()
        if not available:
            raise RuntimeError(detail)
        async with self._lock:
            return await asyncio.to_thread(self._transcribe, path)

    def _transcribe(self, path: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="vivi-stt-") as directory:
            temporary = Path(directory)
            wav_path = temporary / "input.wav"
            output_prefix = temporary / "transcript"
            converted = subprocess.run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(wav_path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if converted.returncode != 0:
                raise RuntimeError(f"ffmpeg conversion failed: {converted.stderr.strip()}")
            completed = subprocess.run(
                [
                    str(self.binary),
                    "-m",
                    str(self.model),
                    "-f",
                    str(wav_path),
                    "-l",
                    self.language,
                    "-nt",
                    "-otxt",
                    "-of",
                    str(output_prefix),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"whisper.cpp failed: {completed.stderr.strip()}")
            transcript_path = output_prefix.with_suffix(".txt")
            if not transcript_path.exists():
                raise RuntimeError("whisper.cpp did not create a transcript")
            return transcript_path.read_text(encoding="utf-8").strip()
