"""Choose a graph provider before a turn starts; never replay graph side effects."""
from __future__ import annotations

import asyncio
import time

import httpx

from src.vivi.config import Settings


class JetsonProviderRouter:
    def __init__(self, config: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport
        self._cached_until = 0.0
        self._cached_ready = False
        self._lock = asyncio.Lock()

    async def _ready(self) -> bool:
        if not self.config.jetson_ai_url or not self.config.jetson_ai_token:
            return False
        if time.monotonic() < self._cached_until:
            return self._cached_ready
        async with self._lock:
            if time.monotonic() < self._cached_until:
                return self._cached_ready
            try:
                async with httpx.AsyncClient(timeout=self.config.jetson_ai_probe_timeout_seconds,
                                             transport=self.transport) as client:
                    response = await client.get(
                        self.config.jetson_ai_url.rstrip("/") + "/v1/capabilities",
                        headers={"Authorization": f"Bearer {self.config.jetson_ai_token}"},
                    )
                response.raise_for_status()
                payload = response.json()
                llm = payload.get("capabilities", {}).get("llm", {})
                self._cached_ready = (
                    payload.get("contract_version") == "1.0"
                    and llm.get("status") == "ready"
                    and llm.get("model_id") == self.config.local_llm_model
                )
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                self._cached_ready = False
            self._cached_until = time.monotonic() + 1.0
            return self._cached_ready

    async def select(self, requested: str, available: set[str]) -> str:
        if self.config.jetson_ai_policy == "disabled" or requested != "local":
            return requested
        if await self._ready():
            return "local"
        if self.config.jetson_ai_policy != "offline_only" and not self.config.rag_local_only:
            for provider in ("google", "openai", "openrouter"):
                if provider in available:
                    return provider
        return "rules"
