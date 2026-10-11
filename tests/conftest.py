import os
import tempfile

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Tests must not inherit provider choices or credentials from a developer's
# .env files. Provider-specific tests construct their own Settings instances.
_test_data = tempfile.TemporaryDirectory(prefix="vivi-tests-")
os.environ.update(
    {
        "APP_ENV": "test",
        "VIVI_DATA_DIR": _test_data.name,
        "RAG_HISTORY_DB": os.path.join(_test_data.name, "history.sqlite3"),
        "VIVI_MEMORY_DB": os.path.join(_test_data.name, "memory.sqlite3"),
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
        "LLM_PROVIDER": "rules",
        "STT_PROVIDER": "off",
        "STT_FALLBACK_PROVIDER": "off",
        "SONIOX_API_KEY": "",
        "SONIOX_LANGUAGE_HINTS": "vi",
        "TTS_PROVIDER": "off",
        "VIVI_VEHICLE_PROVIDER": "memory",
        "RAG_RETRIEVAL_MODE": "sqlite",
        "PHOWHISPER_PRELOAD": "false",
        "ZEROTTS_PRELOAD": "false",
        "OPENAI_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "GOOGLE_API_KEY": "",
        "GEMINI_API_KEY": "",
        # Keep in step with Settings.google_model; tests assert the default.
        "GOOGLE_MODEL": "gemini-3.5-flash-lite",
        "LOCAL_LLM_MODEL": "",
        "VIVI_MEMORY_ENABLED": "false",
    }
)

from src.vivi.api.app import app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for testing API endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
