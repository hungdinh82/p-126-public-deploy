from __future__ import annotations

import asyncio

import httpx
import pytest

from jetson_ai.scheduler import PriorityGpuScheduler
from src.vivi.config import Settings
from src.vivi.connectivity import ConnectivitySupervisor


def _config(tmp_path, **values):
    return Settings(
        data_dir=tmp_path,
        jetson_ai_url="http://jetson.test",
        jetson_ai_token="test-token",
        ai_server_auth_required=True,
        connectivity_failure_threshold=2,
        connectivity_success_threshold=2,
        **values,
    )


@pytest.mark.asyncio
async def test_gateway_hysteresis_and_routing_matrix(tmp_path):
    state = {"internet": True, "jetson": True}

    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clients3.google.com":
            return httpx.Response(204 if state["internet"] else 503)
        if request.url.host == "jetson.test" and state["jetson"]:
            return httpx.Response(200, json={
                "contract_version": "1.0",
                "capabilities": {name: {"status": "ready"} for name in ("stt", "llm", "tts")},
            })
        return httpx.Response(503)

    supervisor = ConnectivitySupervisor(_config(tmp_path), transport=httpx.MockTransport(reply))
    try:
        await supervisor.probe_once()
        available = {"rules", "local", "google", "openai"}
        assert supervisor.snapshot()["effective"]["mode"] == "cloud"
        assert supervisor.llm_chain("auto", available)[:2] == ["google", "openai"]

        state["internet"] = False
        await supervisor.probe_once()
        assert supervisor.internet == "online"  # first miss is tolerated
        await supervisor.probe_once()
        assert supervisor.internet == "offline"
        assert supervisor.llm_chain("auto", available) == ["local"]

        supervisor.set_override("both_down")
        assert await supervisor.select_llm("auto", available) == "rules"
        supervisor.set_override("actual")
        supervisor.set_profile("cloud_only")
        assert await supervisor.select_llm("auto", available) == "rules"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_jetson_capability_override_is_independent(tmp_path):
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clients3.google.com":
            return httpx.Response(204)
        return httpx.Response(200, json={
            "contract_version": "1.0",
            "capabilities": {name: {"status": "ready"} for name in ("stt", "llm", "tts")},
        })

    supervisor = ConnectivitySupervisor(_config(tmp_path), transport=httpx.MockTransport(reply))
    try:
        await supervisor.probe_once()
        supervisor.set_override("stt_down")
        assert not supervisor.capability_available("stt", "jetson")
        assert supervisor.capability_available("llm", "jetson")
        assert supervisor.snapshot()["actual"]["jetson_capabilities"]["stt"] == "ready"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_gpu_scheduler_preempts_tts_and_prioritizes_stt():
    scheduler = PriorityGpuScheduler()
    started = asyncio.Event()
    order = []

    async def tts():
        try:
            async with scheduler.slot("tts"):
                started.set()
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            order.append("tts-cancelled")

    async def job(name):
        async with scheduler.slot(name):
            order.append(name)

    tts_task = asyncio.create_task(tts())
    await started.wait()
    llm_task = asyncio.create_task(job("llm"))
    await asyncio.sleep(0)
    stt_task = asyncio.create_task(job("stt"))
    await asyncio.gather(tts_task, stt_task, llm_task)
    assert order == ["tts-cancelled", "stt", "llm"]
