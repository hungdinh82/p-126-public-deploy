from __future__ import annotations

from src.vivi.config import Settings


def llm_models(config: Settings) -> dict[str, str]:
    """Return the public provider catalog used by API health and the UI."""
    models = {
        "rules": "Kịch bản + LangGraph",
        "openai": config.openai_model,
        "google": config.google_model,
        "local": config.local_llm_model,
        "openrouter": config.openrouter_model,
    }
    local_only = config.rag_local_only or config.jetson_ai_policy == "offline_only"
    return {key: value for key, value in models.items() if not local_only or key in {"rules", "local"}}


def configured_llm_providers(config: Settings) -> list[str]:
    """Return providers whose minimum startup configuration is present."""
    providers = ["rules"]
    if config.openai_api_key:
        providers.append("openai")
    if config.google_api_key:
        providers.append("google")
    if config.local_llm_model:
        providers.append("local")
    if config.openrouter_api_key:
        providers.append("openrouter")
    local_only = config.rag_local_only or config.jetson_ai_policy == "offline_only"
    return [provider for provider in providers if not local_only or provider in {"rules", "local"}]
