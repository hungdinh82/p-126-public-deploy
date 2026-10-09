"""Gateway authentication configuration independent of the Laptop runtime."""

import os
import tomllib
from pathlib import Path


def auth_required() -> bool:
    value = os.environ.get("AI_SERVER_AUTH_REQUIRED", os.environ.get("JETSON_AI_AUTH_REQUIRED"))
    if value is not None:
        if value.lower() not in {"true", "false", "1", "0"}:
            raise ValueError("AI_SERVER_AUTH_REQUIRED must be true or false")
        return value.lower() in {"true", "1"}
    path = Path("config.toml")
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    required = config.get("ai_server_auth_required", True)
    if not isinstance(required, bool):
        raise ValueError("ai_server_auth_required must be a boolean")
    return required
