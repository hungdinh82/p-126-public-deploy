from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("VIVI_HOST", "127.0.0.1")
    port: int = int(os.getenv("VIVI_PORT", "8787"))
    data_dir: Path = Path(os.getenv("VIVI_DATA_DIR", "./data")).resolve()
    store_audio: bool = _bool("VIVI_STORE_AUDIO", True)
    store_transcripts: bool = _bool("VIVI_STORE_TRANSCRIPTS", True)

    stt_provider: str = os.getenv("STT_PROVIDER", "phowhisper")
    phowhisper_model: str = os.getenv("PHOWHISPER_MODEL", "vinai/PhoWhisper-medium")
    phowhisper_device: str = os.getenv("PHOWHISPER_DEVICE", "auto")

    llm_provider: str = os.getenv("LLM_PROVIDER", "rules")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    google_model: str = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
    local_llm_base_url: str = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    local_llm_api_key: str = os.getenv("LOCAL_LLM_API_KEY", "local")
    local_llm_model: str = os.getenv("LOCAL_LLM_MODEL", "")

    tts_provider: str = os.getenv("TTS_PROVIDER", "zerotts")
    zerotts_model: str = os.getenv("ZEROTTS_MODEL", "zeroweight-ai/ZeroTTS")
    zerotts_voice: str = os.getenv("ZEROTTS_VOICE", "VIVI")
    zerotts_device: str = os.getenv("ZEROTTS_DEVICE", "cpu")


settings = Settings()
