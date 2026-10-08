from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource


class Settings(BaseSettings):
    """Single configuration contract shared by API, graph, RAG, and adapters."""

    model_config = SettingsConfigDict(
        env_file=(".env", "data/mqtt/credentials.env"),
        env_file_encoding="utf-8",
        toml_file="config.toml",
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Secrets and per-machine overrides stay in .env; shared runtime defaults
        # live in config.toml so changing providers does not clutter .env.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            TomlConfigSettingsSource(settings_cls),
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
    memory_enabled: bool = Field(default=True, validation_alias="VIVI_MEMORY_ENABLED")
    memory_db: Path | None = Field(default=None, validation_alias="VIVI_MEMORY_DB")
    memory_profile_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$",
                                   validation_alias="VIVI_MEMORY_PROFILE_ID")
    memory_context_limit: int = Field(default=8, ge=1, le=20)
    memory_context_max_characters: int = Field(default=2000, ge=200, le=6000)

    @property
    def memory_path(self) -> Path:
        return self.memory_db or self.data_dir / "memory" / "long_term.sqlite3"

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
    soniox_language_hints: str = "vi"
    soniox_language_hints_strict: bool = False
    # Soniox closes the utterance on a pause instead of waiting for the client.
    soniox_endpoint_detection: bool = True
    soniox_max_endpoint_delay_ms: int = Field(default=1000, ge=500, le=3000)
    soniox_ws_url: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    soniox_timeout_seconds: float = 30
    soniox_stream_max_seconds: float = 60

    llm_provider: Literal["rules", "openai", "google", "local", "openrouter"] = "google"
    llm_timeout_seconds: float = 30
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    google_api_key: str = Field(default="", validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"))
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    google_model: str = "gemini-3.5-flash-lite"
    google_max_tokens: int = Field(default=4096, ge=64, le=8192)
    google_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "qwen/qwen3.7-flash"
    # Qwen3.7 thinks by default; ViVi needs short JSON, so reasoning is off.
    openrouter_reasoning: bool = False
    # Vietnamese answers with citations exceed the 512-token local default.
    openrouter_max_tokens: int = Field(default=1536, ge=64, le=8192)
    local_llm_base_url: str = "http://127.0.0.1:1234/v1"
    local_llm_api_key: str = "local"
    local_llm_model: str = ""
    local_llm_max_tokens: int = Field(default=512, ge=64, le=4096)

    tts_provider: Literal["off", "zerotts"] = "off"
    zerotts_model: str = "zeroweight-ai/ZeroTTS"
    zerotts_voice: str = "VIVI"
    zerotts_device: str = "auto"
    zerotts_preload: bool = False

    vietmap_api_key: str = ""
    # VietMap issues separate consumers: the API key (search/route) stays on the
    # backend, the Tilemap key is embedded in the browser to draw the base map.
    vietmap_tile_key: str = Field(
        default="", validation_alias=AliasChoices("VIETMAP_TILE_KEY", "TILEMAP_API_KEY")
    )
    vietmap_base_url: str = "https://maps.vietmap.vn"
    # tm = street, lm = light, dm = dark (matches the ViVi night theme).
    vietmap_map_style: str = "dm"
    vietmap_timeout_seconds: float = 10
    # Fallback origin when the browser denies geolocation (Hồ Gươm, Hà Nội).
    navigation_default_lat: float = 21.0285
    navigation_default_lng: float = 105.8522

    database_url: str = "sqlite:///./data/app.db"
    rag_data_dir: Path = Path("./data/handbooks")
    rag_history_db: Path = Path("./data/vivi_rag.sqlite3")
    rag_handbook_db: Path = Path("./data/handbooks/handbook.sqlite3")
    rag_retrieval_mode: Literal["sqlite", "sqlite_local", "lexical"] = "sqlite_local"
    rag_local_only: bool = Field(default=True, validation_alias="VIVI_RAG_LOCAL_ONLY")
    rag_local_embedding_dir: Path = Path("./models/multilingual-e5-small-int8")
    rag_embedding_threads: int = Field(default=2, ge=1, le=8)
    rag_embedding_batch_size: int = Field(default=4, ge=1, le=32)
    rag_local_min_similarity: float = Field(default=0.0, ge=0, le=1)
    rag_default_vehicle_model: str = "VF8"
    rag_default_model_year: int = 2026
    rag_default_locale: str = "vi_vn"
    rag_retrieval_k: int = Field(default=12, ge=1, le=50)
    rag_final_k: int = Field(default=5, ge=1, le=12)
    rag_prompt_max_characters: int = Field(default=6000, ge=1000, le=30000)
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
