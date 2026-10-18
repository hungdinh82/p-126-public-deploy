from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.vivi.api.app import create_app
from src.vivi.api.runtime import build_runtime
from src.vivi.config import Settings


async def auth_app(tmp_path, **overrides):
    config = Settings(
        _env_file=None,
        app_env="development",
        auth_required=True,
        data_dir=tmp_path,
        rag_history_db=tmp_path / "history.sqlite3",
        memory_db=tmp_path / "memory.sqlite3",
        memory_enabled=True,
        stt_provider="off",
        tts_provider="off",
        rag_retrieval_mode="sqlite",
        **overrides,
    )
    current = build_runtime(config)
    return current, create_app(config=config, runtime=current)


@pytest.mark.asyncio
async def test_login_creates_server_owned_conversation_and_isolates_accounts(tmp_path):
    runtime, app = await auth_app(tmp_path)
    transport = ASGITransport(app=app)
    try:
        async with (
            AsyncClient(transport=transport, base_url="http://test") as first,
            AsyncClient(transport=transport, base_url="http://test") as second,
        ):
            one = await first.post("/api/v1/session/login", json={"phone": "0901 234 567", "pin": "1234"})
            assert one.status_code == 200
            conversation = one.json()["conversation_id"]
            assert conversation
            remembered = await first.put("/api/v1/memory/display_name", json={"kind": "fact", "value": "An"})
            assert remembered.status_code == 200
            another = await first.post("/api/v1/session/conversations")
            assert another.status_code == 201
            assert another.json()["conversation_id"] != conversation

            two = await second.post("/api/v1/session/login", json={"phone": "0909 876 543", "pin": "9876"})
            assert two.status_code == 200
            assert (await second.get("/api/v1/memory")).json()["items"] == []
            stolen = await second.get(f"/api/v2/navigation/snapshot?session_id={conversation}")
            assert stolen.status_code == 404
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_wrong_pin_is_rejected_and_logout_invalidates_cookie(tmp_path):
    runtime, app = await auth_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (
                await client.post("/api/v1/session/login", json={"phone": "0901 234 567", "pin": "1234"})
            ).status_code == 200
            await client.delete("/api/v1/session")
            assert (await client.get("/api/v1/session")).status_code == 401
            wrong = await client.post("/api/v1/session/login", json={"phone": "0901 234 567", "pin": "9999"})
            assert wrong.status_code == 401
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_engineer_role_comes_from_operator_configuration(tmp_path):
    runtime, app = await auth_app(tmp_path, engineer_phone="0908 111 222", engineer_pin="2468")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post("/api/v1/session/login", json={"phone": "0908 111 222", "pin": "2468"})
            assert login.status_code == 200
            assert login.json()["role"] == "engineer"
            assert login.json()["conversation_id"] is None
            assert (await client.get("/api/v1/engineer/fleet")).status_code == 200
            assert (await client.post("/api/v1/session/conversations")).status_code == 403
    finally:
        await runtime.aclose()
