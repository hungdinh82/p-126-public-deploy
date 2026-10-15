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
        "LLM_PROVIDER": "google",
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
        "VIETMAP_API_KEY": "",
        "TAVILY_API_KEY": "",
        "VIENEU_API_KEY": "",
        "VIVI_MEMORY_ENABLED": "false",
    }
)

from src.vivi.api.app import create_app  # noqa: E402
from src.vivi.api.runtime import active_runtime, build_runtime  # noqa: E402
from src.vivi.config import Settings  # noqa: E402


@pytest_asyncio.fixture
async def client(tmp_path):
    """Each API test owns its runtime and closes all workers after the request."""
    config = Settings(
        _env_file=None,
        data_dir=tmp_path,
        rag_history_db=tmp_path / "history.sqlite3",
        memory_db=tmp_path / "memory.sqlite3",
        stt_provider="off",
        tts_provider="off",
        llm_provider="google",
        rag_retrieval_mode="sqlite",
    )
    current = build_runtime(config)
    application = create_app(config=config, runtime=current)
    token = active_runtime.set(current)
    try:
        async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
            yield ac
    finally:
        await current.aclose()
        active_runtime.reset(token)


def pytest_addoption(parser):
    parser.addoption("--run-mqtt", action="store_true", default=False)
    parser.addoption("--run-live", action="store_true", default=False)
    parser.addoption("--run-hardware", action="store_true", default=False)


def pytest_collection_modifyitems(config, items):
    import pytest

    for item in items:
        if item.path.name == "test_mqtt_vehicle.py":
            item.add_marker(pytest.mark.mqtt)
        for marker in ("mqtt", "live", "hardware"):
            if marker in item.keywords and not config.getoption("--run-" + marker):
                item.add_marker(pytest.mark.skip(reason="Enable with --run-" + marker))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch, request):
    """Default tests may use loopback and mock transports, never external services."""
    if "live" in request.node.keywords and request.config.getoption("--run-live"):
        return
    import socket

    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}:
            raise AssertionError("External networking is disabled in default tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
