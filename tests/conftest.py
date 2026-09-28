import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.vivi.api.app import app


@pytest_asyncio.fixture
async def client():
    """Async HTTP client for testing API endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
