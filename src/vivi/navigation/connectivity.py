from __future__ import annotations

from src.vivi.navigation.vietmap import VietMapError


class ConnectivityMapClient:
    """Blocks new cloud map work offline while leaving stored journeys untouched."""

    def __init__(self, client, connectivity) -> None:
        self.client = client
        self.connectivity = connectivity

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _require_online(self) -> None:
        if not self.connectivity.capability_available("rag", "cloud"):
            raise VietMapError("Đang offline: tuyến hiện tại vẫn dùng được nhưng không thể tìm hoặc lập tuyến mới.")

    async def search(self, *args, **kwargs):
        self._require_online()
        return await self.client.search(*args, **kwargs)

    async def place(self, *args, **kwargs):
        self._require_online()
        return await self.client.place(*args, **kwargs)

    async def route(self, *args, **kwargs):
        self._require_online()
        return await self.client.route(*args, **kwargs)

    async def matrix(self, *args, **kwargs):
        self._require_online()
        return await self.client.matrix(*args, **kwargs)

    async def probe_tiles(self, *args, **kwargs):
        self._require_online()
        return await self.client.probe_tiles(*args, **kwargs)
