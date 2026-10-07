from __future__ import annotations

from src.vivi.config import Settings


def llm_models(config: Settings) -> dict[str, str]:
    """Return the public provider catalog used by API health and the UI."""
    models = {
        "rules": "Kịch bản + LangGraph",
        "openai": config.openai_model,
        "local": config.local_llm_model,
        "openrouter": config.openrouter_model,
    }
    return {key: value for key, value in models.items() if not config.rag_local_only or key in {"rules", "local"}}


def configured_llm_providers(config: Settings) -> list[str]:
    """Return providers whose minimum startup configuration is present."""
    providers = ["rules"]
    if config.openai_api_key:
        providers.append("openai")
    if config.local_llm_model:
        providers.append("local")
    if config.openrouter_api_key:
        providers.append("openrouter")
    return [provider for provider in providers if not config.rag_local_only or provider in {"rules", "local"}]
