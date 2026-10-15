from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Literal

import httpx

from src.vivi.config import Settings
from src.vivi.connectivity.store import ConnectivityEventStore

Connectivity = Literal["checking", "online", "offline", "not_configured"]
Capability = Literal["stt", "llm", "tts", "rag"]

PROFILES = {"auto", "cloud_only", "jetson_only", "rules_only"}
OVERRIDES = {
    "actual", "internet_down", "jetson_down", "both_down",
    "stt_down", "llm_down", "tts_down",
}


class ConnectivitySupervisor:
    """Owns observed connectivity and derives one effective routing state.

    Probes only report reachability. Provider failures are recorded separately so
    one cloud API failure cannot incorrectly mark the whole Internet offline.
    """

    def __init__(self, config: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.transport = transport
        self.internet: Connectivity = "checking"
        self.jetson: Connectivity = "checking" if config.jetson_ai_url else "not_configured"
        self.capabilities: dict[str, str] = {
            name: "checking" if config.jetson_ai_url else "not_configured"
            for name in ("stt", "llm", "tts")
        }
        self.accepting_requests = {name: True for name in self.capabilities}
        self.profile = "auto"
        self.override = "actual"
        self.default_interface: str | None = None
        self._failures = {"internet": 0, "jetson": 0}
        self._successes = {"internet": 0, "jetson": 0}
        self._task: asyncio.Task | None = None
        self._has_observation = False
        self._closed = False
        self._last_cleanup = time.monotonic()
        self.store = ConnectivityEventStore(
            config.connectivity_db_path, config.connectivity_retention_days
        )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._closed = False
        await self.probe_once()
        self._task = asyncio.create_task(self._loop(), name="vivi-connectivity-monitor")

    async def stop(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        # A stopped monitor has no current observations. Reusing the lifecycle
        # must probe again instead of routing with stale readiness.
        self._has_observation = False
        self.internet = 'checking'
        self.jetson = 'checking' if self.config.jetson_ai_url else 'not_configured'
        self.capabilities = {name: 'checking' for name in self.capabilities}

    async def wait_ready(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not self._has_observation and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    async def _loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.config.connectivity_probe_interval_seconds)
            await self.probe_once()
            if time.monotonic() - self._last_cleanup >= 86400:
                self.store.cleanup()
                self._last_cleanup = time.monotonic()

    async def probe_once(self) -> None:
        internet, jetson = await asyncio.gather(
            self._probe_internet(), self._probe_jetson(), return_exceptions=True
        )
        self._apply_observation("internet", internet is True)
        if self.config.jetson_ai_url:
            self._apply_observation("jetson", isinstance(jetson, dict))
            if isinstance(jetson, dict):
                values = jetson.get("capabilities", {})
                for name in self.capabilities:
                    payload = values.get(name, {})
                    status = str(payload.get("status", "unavailable"))
                    self.capabilities[name] = status
                    self.accepting_requests[name] = bool(payload.get('accepting_requests', status == 'ready'))
                    if name == 'llm' and payload.get('model_id') not in {None, self.config.local_llm_model}:
                        self.accepting_requests[name] = False
            elif self.jetson == "offline":
                self.capabilities = {name: "unavailable" for name in self.capabilities}
        self._has_observation = True

    def _apply_observation(self, component: str, ok: bool) -> None:
        current = getattr(self, component)
        self._successes[component] = self._successes[component] + 1 if ok else 0
        self._failures[component] = self._failures[component] + 1 if not ok else 0
        next_state = current
        if ok and (current == "checking" or self._successes[component] >= self.config.connectivity_success_threshold):
            next_state = "online"
        elif not ok and self._failures[component] >= self.config.connectivity_failure_threshold:
            next_state = "offline"
        if next_state != current:
            setattr(self, component, next_state)
            self.store.append("state_change", component, next_state, previous=current)

    def _linux_default_interface(self) -> str | None:
        route = Path("/proc/net/route")
        if not route.exists():
            return None
        try:
            for line in route.read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return fields[0]
        except (OSError, ValueError):
            return None
        return None

    async def _probe_internet(self) -> bool:
        self.default_interface = await asyncio.to_thread(self._linux_default_interface)
        if self.default_interface:
            operstate = Path("/sys/class/net") / self.default_interface / "operstate"
            try:
                if operstate.read_text().strip() not in {"up", "unknown"}:
                    return False
            except OSError:
                pass
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.config.internet_probe_timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                urls = [self.config.internet_probe_url]
                for provider in ('google', 'openai', 'openrouter'):
                    if getattr(self.config, provider + '_api_key', ''):
                        urls.append(getattr(self.config, provider + '_base_url'))
                ok = False
                for index, url in enumerate(dict.fromkeys(urls)):
                    try:
                        response = await client.get(url)
                        # An authenticated API returning 401/403/404 is reachable;
                        # credentials/quotas belong to the provider, not Internet.
                        ok = response.status_code in ({200, 204} if index == 0 else {200, 204, 401, 403, 404})
                    except httpx.HTTPError:
                        continue
                    if ok:
                        break
        except httpx.HTTPError:
            ok = False
        self.store.append(
            "probe", "internet", "success" if ok else "failure",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return ok

    async def _probe_jetson(self) -> dict[str, Any] | None:
        if not self.config.jetson_ai_url:
            return None
        headers = {}
        if self.config.ai_server_auth_required and self.config.jetson_ai_token:
            headers["Authorization"] = f"Bearer {self.config.jetson_ai_token}"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.config.jetson_ai_probe_timeout_seconds, transport=self.transport
            ) as client:
                response = await client.get(
                    self.config.jetson_ai_url.rstrip("/") + "/v1/capabilities", headers=headers
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("contract_version") != "1.0":
                raise ValueError("unsupported capability contract")
            values = payload.get('capabilities')
            if not isinstance(values, dict) or any(not isinstance(values.get(name, {}), dict) for name in self.capabilities):
                raise ValueError('invalid capabilities payload')
        except (httpx.HTTPError, ValueError, TypeError):
            payload = None
        self.store.append(
            "probe", "jetson", "success" if payload else "failure",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return payload

    def set_profile(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError("Model profile không hợp lệ")
        previous, self.profile = self.profile, profile
        self.store.append("profile", "gateway", "changed", previous=previous, profile=profile)

    def record_provider_result(
        self, provider: str, outcome: str, latency_ms: float, reason: str | None
    ) -> None:
        if provider in {"google", "openai", "openrouter"} and outcome == "served":
            previous = self.internet
            self.internet = "online"
            self._failures["internet"] = 0
            self._successes["internet"] = self.config.connectivity_success_threshold
            if previous != "online":
                self.store.append("state_change", "internet", "online", previous=previous,
                                  reason="cloud_provider_success")
        self.store.append(
            "provider_result", "llm", outcome, provider=provider,
            latency_ms=latency_ms, reason=reason,
        )

    def set_override(self, override: str) -> None:
        if override not in OVERRIDES:
            raise ValueError("Connectivity scenario không hợp lệ")
        previous, self.override = self.override, override
        self.store.append("override", "gateway", "changed", previous=previous, override=override)

    def _effective(self) -> tuple[Connectivity, Connectivity, dict[str, str]]:
        internet, jetson = self.internet, self.jetson
        capabilities = dict(self.capabilities)
        if self.override in {"internet_down", "both_down"}:
            internet = "offline"
        if self.override in {"jetson_down", "both_down"}:
            jetson = "offline"
            capabilities = {name: "unavailable" for name in capabilities}
        capability = self.override.removesuffix("_down")
        if capability in capabilities:
            capabilities[capability] = "unavailable"
        return internet, jetson, capabilities

    def capability_available(self, capability: Capability, location: str) -> bool:
        internet, jetson, capabilities = self._effective()
        if location == "cloud":
            return (internet == "online" and self.profile not in {"jetson_only", "rules_only"}
                    and self.config.jetson_ai_policy != 'offline_only'
                    and (capability != 'llm' or not self.config.rag_local_only))
        if location == "jetson":
            return (
                jetson == "online"
                and capabilities.get(capability) in {"ready", "busy"}
                and self.accepting_requests.get(capability, False)
                and self.profile not in {"cloud_only", "rules_only"}
            )
        return True

    def llm_chain(self, requested: str | None, available: set[str]) -> list[str]:
        cloud = [name for name in ("google", "openai", "openrouter") if name in available]
        standalone = (self.config.jetson_ai_policy == 'disabled' and self.profile not in {'cloud_only', 'rules_only'})
        local = ["local"] if "local" in available and (standalone or self.capability_available("llm", "jetson")) else []
        if self.profile == "rules_only":
            return []
        preference = None if requested in {None, "auto"} else requested
        if preference == "rules":
            return []
        ordered: list[str]
        if preference == "local":
            ordered = [*local, *cloud]
        elif preference in cloud:
            ordered = [preference, *[name for name in cloud if name != preference], *local]
        else:
            internet, _, _ = self._effective()
            ordered = [*cloud, *local] if internet == "online" else [*local, *cloud]
        if not self.capability_available("llm", "cloud"):
            ordered = [name for name in ordered if name == "local"]
        return list(dict.fromkeys(ordered))

    async def select_llm(self, requested: str | None, available: set[str]) -> str:
        if requested not in {None, "auto"}:
            # A manual preference remains a preference, then the resolver uses
            # the dynamic chain for safe fallback.
            if requested == "rules":
                return "rules"
        elif self._task is not None:
            await self.wait_ready()
        chain = self.llm_chain(requested, available)
        selected = chain[0] if chain else "rules"
        self.store.append(
            "selection", "llm", "selected", provider=selected,
            reason=f"profile={self.profile}; override={self.override}",
        )
        return selected

    @property
    def observed(self):
        return self._has_observation

    @property
    def routing_active(self):
        return self.observed or self.profile != 'auto' or self.override != 'actual'

    def snapshot(self) -> dict[str, Any]:
        effective_internet, effective_jetson, capabilities = self._effective()
        mode = (
            "cloud" if self.capability_available('llm', 'cloud')
            else "jetson" if self.capability_available('llm', 'jetson')
            else "rules"
        )
        return {
            "actual": {
                "internet": self.internet,
                "jetson": self.jetson,
                "jetson_capabilities": dict(self.capabilities),
                "default_interface": self.default_interface,
            },
            "effective": {
                "internet": effective_internet,
                "jetson": effective_jetson,
                "jetson_capabilities": capabilities,
                "mode": mode,
            },
            "profile": self.profile,
            "override": self.override,
            "simulated": self.override != "actual",
        }
