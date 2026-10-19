from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, field_validator, model_validator
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
        if __import__("os").environ.get("APP_ENV") == "test" and dotenv_settings.env_file == cls.model_config.get(
            "env_file"
        ):
            return (init_settings, env_settings, file_secret_settings, TomlConfigSettingsSource(settings_cls))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            TomlConfigSettingsSource(settings_cls),
        )

    runtime_profile: Literal["pc", "nano", "test"] = Field(default="pc", validation_alias="VIVI_RUNTIME_PROFILE")
    app_name: str = "VIVI Cabin Copilot"
    app_env: Literal["development", "production", "test"] = "development"
    host: str = Field(default="127.0.0.1", validation_alias="VIVI_HOST")
    port: int = Field(default=8787, validation_alias="VIVI_PORT", ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = "http://127.0.0.1:8787,http://localhost:8787"

    auth_required: bool = Field(default=True, validation_alias="VIVI_AUTH_REQUIRED")
    auth_session_hours: int = Field(default=8, ge=1, le=168, validation_alias="VIVI_AUTH_SESSION_HOURS")
    engineer_phone: str = Field(default="", validation_alias="VIVI_ENGINEER_PHONE")
    engineer_pin: str = Field(default="", validation_alias="VIVI_ENGINEER_PIN")
    demo_mode: bool = Field(default=False, validation_alias="VIVI_DEMO_MODE")

    data_dir: Path = Field(default=Path("./data"), validation_alias="VIVI_DATA_DIR")
    turn_timeout_seconds: float = Field(default=120, ge=1, le=180)
    cloud_budget_seconds: float = Field(default=10, ge=0.1, le=60)
    local_budget_seconds: float = Field(default=30, ge=0.1, le=60)
    semantic_budget_seconds: float = Field(default=40, ge=0.1, le=60)
    provider_breaker_seconds: float = Field(default=30, ge=0, le=300)
    planner_max_tokens: int = Field(default=2048, ge=512, le=8192)
    conversation_enabled: bool = True
    information_enabled: bool = True
    tavily_api_key: str = ""
    tavily_monthly_limit: int = Field(default=900, ge=0, le=1000)
    information_timeout_seconds: float = Field(default=12, ge=1, le=30)
    information_http_timeout_seconds: float = Field(default=4, ge=0.1, le=10)
    information_max_results: int = Field(default=5, ge=1, le=5)
    information_timezone: str = "Asia/Ho_Chi_Minh"
    weather_default_location: str = Field(default="Hà Nội", min_length=1, max_length=200)

    @field_validator("information_timezone")
    @classmethod
    def valid_information_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("information_timezone must be a valid IANA timezone") from exc
        return value

    store_audio: bool = Field(default=False, validation_alias="VIVI_STORE_AUDIO")
    store_transcripts: bool = Field(default=False, validation_alias="VIVI_STORE_TRANSCRIPTS")
    memory_enabled: bool = Field(default=True, validation_alias="VIVI_MEMORY_ENABLED")
    memory_db: Path | None = Field(default=None, validation_alias="VIVI_MEMORY_DB")
    memory_profile_id: str = Field(
        default="default", pattern=r"^[a-zA-Z0-9_-]{1,64}$", validation_alias="VIVI_MEMORY_PROFILE_ID"
    )
    memory_context_limit: int = Field(default=8, ge=1, le=20)
    memory_context_max_characters: int = Field(default=2000, ge=200, le=6000)

    @property
    def memory_path(self) -> Path:
        return self.memory_db or self.data_dir / "memory" / "long_term.sqlite3"

    @property
    def auth_db_path(self) -> Path:
        return self.data_dir / "runtime" / "auth.sqlite3"

    vehicle_provider: Literal["memory", "mqtt"] = Field(default="memory", validation_alias="VIVI_VEHICLE_PROVIDER")
    vehicle_id: str = Field(default="demo-car-1", validation_alias="VIVI_VEHICLE_ID")
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_api_username: str = ""
    mqtt_api_password: str = ""
    mqtt_timeout_seconds: float = 3.0
    vehicle_simulator_url: str = Field(default="http://127.0.0.1:8788", validation_alias="VIVI_VEHICLE_SIMULATOR_URL")

    stt_provider: Literal["off", "phowhisper", "whisper_cpp", "soniox", "jetson"] = "soniox"
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

    llm_provider: Literal["openai", "google", "local", "openrouter"] = "google"
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
    local_semantic_transport: Literal["json_schema", "native"] = "json_schema"
    local_semantic_contract: Literal["legacy", "operations_v1"] = "operations_v1"
    # Opt-in Laptop router for the private Jetson inference gateway.
    jetson_ai_policy: Literal["disabled", "offline_only", "prefer_local", "allow_cloud"] = "disabled"
    jetson_ai_url: str = Field(
        default="", validation_alias=AliasChoices("AI_SERVER_URL", "JETSON_AI_URL", "ai_server_url", "jetson_ai_url")
    )
    jetson_ai_token: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SERVER_TOKEN", "JETSON_AI_TOKEN", "ai_server_token", "jetson_ai_token"),
    )
    jetson_ai_probe_timeout_seconds: float = Field(default=0.75, gt=0, le=5)
    # Speech transport only. ModelGateway retains the per-model and turn budgets.
    ai_server_request_timeout_seconds: float = Field(default=180, ge=1, le=300)
    internet_probe_url: str = "https://clients3.google.com/generate_204"
    internet_probe_timeout_seconds: float = Field(default=1.5, gt=0, le=10)
    connectivity_probe_interval_seconds: float = Field(default=2.0, ge=0.5, le=30)
    connectivity_failure_threshold: int = Field(default=2, ge=1, le=10)
    connectivity_success_threshold: int = Field(default=2, ge=1, le=10)
    connectivity_retention_days: int = Field(default=7, ge=1, le=90)

    @property
    def connectivity_db_path(self) -> Path:
        return self.data_dir / "runtime" / "connectivity.sqlite3"

    ai_server_auth_required: bool = Field(
        default=True,
        validation_alias=AliasChoices("AI_SERVER_AUTH_REQUIRED", "JETSON_AI_AUTH_REQUIRED", "ai_server_auth_required"),
    )

    @model_validator(mode="after")
    def configure_ai_server(self) -> Settings:
        # Gateway capabilities and inference must use the same server/token.
        # Disabled policy preserves standalone OpenAI-compatible local servers.
        if self.jetson_ai_policy != "disabled" and self.jetson_ai_url:
            url = self.jetson_ai_url.strip().rstrip("/")
            if url.endswith("/v1"):
                url = url[:-3]
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("AI_SERVER_URL must be an HTTP(S) gateway URL without credentials, query or fragment")
            self.jetson_ai_url = url
            self.local_llm_base_url = url + "/v1"
            if not self.ai_server_auth_required:
                self.local_llm_api_key = ""
                self.jetson_ai_token = ""
            elif self.jetson_ai_token:
                self.local_llm_api_key = self.jetson_ai_token
            elif self.local_llm_api_key and self.local_llm_api_key != "local":
                self.jetson_ai_token = self.local_llm_api_key
        return self

    tts_provider: Literal["off", "zerotts", "vieneu"] = "off"
    zerotts_model: str = "zeroweight-ai/ZeroTTS"
    zerotts_voice: str = "VIVI"
    zerotts_device: str = "auto"
    zerotts_preload: bool = False
    vieneu_api_key: str = Field(default="", validation_alias=AliasChoices("VIENEU_API_KEY", "VIETNEUTTS_API_KEY"))
    vieneu_base_url: str = "https://api.vieneu.io/api/v1"
    vieneu_voice: str = "Mai Bé Phương"
    # PCM at 24 kHz is VieNeu's default rate and half the bytes of 48 kHz.
    vieneu_sample_rate: Literal[8000, 16000, 22050, 24000, 44100, 48000] = 24000
    vieneu_timeout_seconds: float = 30

    vietmap_api_key: str = ""
    # VietMap issues separate consumers: the API key (search/route) stays on the
    # backend, the Tilemap key is embedded in the browser to draw the base map.
    vietmap_tile_key: str = Field(default="", validation_alias=AliasChoices("VIETMAP_TILE_KEY", "TILEMAP_API_KEY"))
    vietmap_base_url: str = "https://maps.vietmap.vn"
    # tm = street, lm = light, dm = dark (matches the ViVi night theme).
    vietmap_map_style: str = "dm"
    vietmap_timeout_seconds: float = 10
    # Fallback origin when the browser denies geolocation (Hồ Gươm, Hà Nội).
    navigation_default_lat: float = 21.0285
    navigation_default_lng: float = 105.8522
    navigation_default_label: str = "Hồ Gươm"
    energy_profile_path: Path | None = Path("data/energy/vf8-plus.json")
    charging_stations_path: Path | None = Path("data/energy/charging-stations.json")
    navigation_pharmacy_category: str = ""
    navigation_charging_category: str = ""
    navigation_charging_search_seconds: float = Field(default=8, ge=1, le=15)
    navigation_charging_radius_m: int = Field(default=20000, ge=1000, le=40000)
    navigation_charging_max_detour_m: int = Field(default=15000, ge=1000, le=40000)
    charging_assumptions_path: Path | None = Path("data/energy/charging-assumptions.json")
    charging_assumptions_enabled: bool = True

    database_url: str = "sqlite:///./data/app.db"
    rag_data_dir: Path = Path("./data/handbooks")
    rag_history_db: Path = Path("./data/vivi_history.sqlite3")
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
