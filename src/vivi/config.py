from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single configuration contract shared by API, graph, RAG, and adapters."""

    model_config = SettingsConfigDict(
        env_file=(".env", "data/mqtt/credentials.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    runtime_profile: Literal["pc", "nano", "test"] = Field(
        default="pc", validation_alias="VIVI_RUNTIME_PROFILE"
    )
    app_name: str = "VIVI Cabin Copilot"
    app_env: Literal["development", "production", "test"] = "development"
    host: str = Field(default="127.0.0.1", validation_alias="VIVI_HOST")
    port: int = Field(default=8787, validation_alias="VIVI_PORT", ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = "http://127.0.0.1:8787,http://localhost:8787"

    data_dir: Path = Field(default=Path("./data"), validation_alias="VIVI_DATA_DIR")
    store_audio: bool = Field(default=False, validation_alias="VIVI_STORE_AUDIO")
    store_transcripts: bool = Field(default=False, validation_alias="VIVI_STORE_TRANSCRIPTS")

    vehicle_provider: Literal["memory", "mqtt"] = Field(
        default="memory", validation_alias="VIVI_VEHICLE_PROVIDER"
    )
    vehicle_id: str = Field(default="demo-car-1", validation_alias="VIVI_VEHICLE_ID")
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_api_username: str = ""
    mqtt_api_password: str = ""
    mqtt_timeout_seconds: float = 3.0

    stt_provider: Literal["off", "phowhisper", "whisper_cpp", "soniox"] = "soniox"
    # Local STT used when the primary (cloud) provider is missing or fails.
    stt_fallback_provider: Literal["off", "phowhisper", "whisper_cpp"] = "phowhisper"
    # Warm the fallback in a background task so startup is not blocked.
    stt_fallback_preload: bool = True
    phowhisper_model: str = "vinai/PhoWhisper-medium"
    phowhisper_device: str = "auto"
    phowhisper_dtype: str = "auto"
    phowhisper_preload: bool = False
    phowhisper_language: str = "vi"
    whisper_cpp_binary: str = "./models/whisper.cpp/build/bin/whisper-cli"
    whisper_cpp_model: Path = Path("./models/whisper.cpp/models/ggml-base.bin")
    ffmpeg_binary: str = "ffmpeg"
    soniox_api_key: str = ""
    soniox_model: str = "stt-rt-v5"
    soniox_language_hints: str = "vi,en"
    soniox_ws_url: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    soniox_timeout_seconds: float = 30
    soniox_stream_max_seconds: float = 60

    llm_provider: Literal["rules", "openai", "google", "local", "openrouter"] = "rules"
    llm_timeout_seconds: float = 30
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "qwen/qwen3.7-flash"
    # Qwen3.7 thinks by default; ViVi needs short JSON, so reasoning is off.
    openrouter_reasoning: bool = False
    # Vietnamese answers with citations exceed the 512-token local default.
    openrouter_max_tokens: int = Field(default=1536, ge=64, le=8192)
    google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"
    local_llm_base_url: str = "http://127.0.0.1:1234/v1"
    local_llm_api_key: str = "local"
    local_llm_model: str = ""
    local_llm_max_tokens: int = Field(default=512, ge=64, le=4096)

    tts_provider: Literal["off", "zerotts"] = "off"
    zerotts_model: str = "zeroweight-ai/ZeroTTS"
    zerotts_voice: str = "VIVI"
    zerotts_device: str = "auto"
    zerotts_preload: bool = False

    database_url: str = "sqlite:///./data/app.db"
    chroma_persist_dir: str = "./data/chroma"
    rag_generation_model: str = "gemini-2.5-flash"
    rag_embedding_model: str = "gemini-embedding-001"
    rag_embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    rag_data_dir: Path = Path("./data/handbooks")
    rag_history_db: Path = Path("./data/vivi_rag.sqlite3")
    rag_handbook_db: Path = Path("./data/handbooks/handbook.sqlite3")
    rag_collection_name: str = "vivi_handbook"
    rag_retrieval_mode: Literal["sqlite", "lexical", "hybrid"] = "sqlite"
    rag_default_vehicle_model: str = "VF8"
    rag_default_model_year: int = 2026
    rag_default_locale: str = "vi_vn"
    rag_retrieval_k: int = Field(default=12, ge=1, le=50)
    rag_final_k: int = Field(default=5, ge=1, le=12)
    rag_prompt_max_characters: int = Field(default=6000, ge=1000, le=30000)
    rag_max_cosine_distance: float = Field(default=0.62, ge=0, le=2)
    rag_history_turns: int = Field(default=6, ge=0, le=20)

    @property
    def app_host(self) -> str:
        return self.host

    @property
    def app_port(self) -> int:
        return self.port


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
