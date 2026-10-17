from pathlib import Path

import pytest
from pydantic import ValidationError

from src.vivi.config import Settings


def test_single_gateway_url_and_token_from_toml_and_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        'ai_server_url = "http://localhost:8789/"\njetson_ai_policy = "prefer_local"\n', encoding="utf-8"
    )
    assert Settings(_env_file=None).local_llm_base_url == "http://localhost:8789/v1"
    monkeypatch.setenv("AI_SERVER_URL", "https://gpu.example.test/ai/v1/")
    monkeypatch.setenv("AI_SERVER_TOKEN", "test-token")
    config = Settings(_env_file=None)
    assert config.jetson_ai_url == "https://gpu.example.test/ai"
    assert config.local_llm_base_url == "https://gpu.example.test/ai/v1"
    assert config.local_llm_api_key == config.jetson_ai_token == "test-token"


def test_legacy_explicit_settings_override_shared_toml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('ai_server_url = "http://localhost:8789"\n', encoding="utf-8")
    config = Settings(
        _env_file=None,
        jetson_ai_policy="prefer_local",
        jetson_ai_url="http://legacy.test:8789",
        jetson_ai_token="legacy",
    )
    assert config.local_llm_base_url == "http://legacy.test:8789/v1"
    assert config.local_llm_api_key == "legacy"


def test_legacy_environment_names(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JETSON_AI_URL", "http://legacy.test:8789")
    monkeypatch.setenv("JETSON_AI_TOKEN", "legacy-token")
    config = Settings(_env_file=None, jetson_ai_policy="prefer_local")
    assert config.local_llm_base_url == "http://legacy.test:8789/v1"
    assert config.local_llm_api_key == "legacy-token"


def test_disabled_gateway_keeps_standalone_local_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None,
        jetson_ai_policy="disabled",
        ai_server_url="http://gateway.test:8789",
        local_llm_base_url="http://lmstudio.test:1234/v1",
    )
    assert config.local_llm_base_url == "http://lmstudio.test:1234/v1"


@pytest.mark.parametrize("url", ["file:///tmp/model", "http://", "http://user:secret@host", "http://host?token=x"])
def test_invalid_gateway_url_rejected(url, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="AI_SERVER_URL"):
        Settings(_env_file=None, jetson_ai_policy="prefer_local", ai_server_url=url)


def test_disabled_auth_does_not_send_old_token(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = Settings(
        _env_file=None,
        jetson_ai_policy="prefer_local",
        ai_server_url="http://gpu.test:8789",
        ai_server_auth_required=False,
        jetson_ai_token="old-token",
        local_llm_api_key="old-key",
    )
    assert config.local_llm_api_key == config.jetson_ai_token == ""
