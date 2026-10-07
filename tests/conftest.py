import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Tests must not inherit provider choices or credentials from a developer's
# .env files. Provider-specific tests construct their own Settings instances.
os.environ.update(
    {
        "APP_ENV": "test",
        "LLM_PROVIDER": "rules",
        "STT_PROVIDER": "off",
        "STT_FALLBACK_PROVIDER": "off",
        "SONIOX_API_KEY": "",
        "TTS_PROVIDER": "off",
        "VIVI_VEHICLE_PROVIDER": "memory",
        "RAG_RETRIEVAL_MODE": "sqlite",
        "PHOWHISPER_PRELOAD": "false",
        "ZEROTTS_PRELOAD": "false",
        "OPENAI_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "LOCAL_LLM_MODEL": "",
    }
)

from src.vivi.api.app import app


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for testing API endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
