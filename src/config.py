from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "AI20K Agent"
    app_env: Literal["development", "production", "test"] = "development"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_host: str = "0.0.0.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = "http://localhost:3000"

    # LLM
    openai_api_key: str = ""
    model_name: str = "gpt-4o-mini"
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)

    # Database
    database_url: str = "sqlite:///./data/app.db"

    # Vector Store
    chroma_persist_dir: str = "./data/chroma"

    # ViVi handbook RAG
    google_api_key: str = ""
    rag_generation_model: str = "gemini-3.1-flash-lite"
    rag_embedding_model: str = "gemini-embedding-001"
    rag_embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    rag_data_dir: Path = Path("./data/handbooks")
    rag_history_db: Path = Path("./data/vivi_rag.sqlite3")
    rag_collection_name: str = "vivi_handbook"
    rag_default_vehicle_model: str = "VF8"
    rag_default_model_year: int = 2026
    rag_default_locale: str = "vi_vn"
    rag_retrieval_k: int = Field(default=12, ge=1, le=50)
    rag_final_k: int = Field(default=5, ge=1, le=12)
    rag_max_cosine_distance: float = Field(default=0.62, ge=0, le=2)
    rag_history_turns: int = Field(default=6, ge=0, le=20)


@lru_cache
def get_settings() -> Settings:
    return Settings()
