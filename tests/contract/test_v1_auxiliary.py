from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_uses_canonical_edge_app(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["orchestration"]["engine"] == "langgraph"
    assert payload["orchestration"]["retrieval"] == "sqlite_fts5"


@pytest.mark.asyncio
async def test_edge_metrics_is_available_without_enabling_transcript_storage(client):
    response = await client.get("/api/v1/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert "memory" in payload
    assert "gpu" in payload
    assert payload["pipeline"]["status"] == "chưa có lượt chạy"
