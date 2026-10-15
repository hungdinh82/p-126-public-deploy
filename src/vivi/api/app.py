from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.vivi.api.routes import (
    core,
    engineer,
    health,
    history,
    memory,
    metrics,
    navigation,
    roles,
    speech,
    turns,
    vehicle,
    web,
)
from src.vivi.api.runtime import active_runtime, build_runtime, runtime
from src.vivi.config import get_settings

logger = logging.getLogger(__name__)


async def _preload_stt_fallback() -> None:
    try:
        await runtime.stt.preload_fallback()
        logger.info("STT dự phòng đã tải xong")
    except Exception:
        logger.exception("Không tải được STT dự phòng")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "runtime"):
        app.state.runtime = app.state.runtime_factory(app.state.settings)
    token = active_runtime.set(app.state.runtime)
    metrics.start_gpu_sampler()
    config = runtime.settings
    background = []
    try:
        if config.tts_provider != "off" and config.zerotts_preload:
            await runtime.tts.preload()
        if config.stt_provider != "off" and config.phowhisper_preload:
            await runtime.stt.preload()
        if config.stt_fallback_preload and hasattr(runtime.stt, "preload_fallback"):
            background.append(asyncio.create_task(_preload_stt_fallback()))
        client = getattr(runtime.orchestrator, "local_semantic_client", None)
        if client is not None and config.jetson_ai_url and config.jetson_ai_policy != "disabled":
            from src.vivi.dialogue.planner import warmup_local_client

            try:
                await warmup_local_client(client)
                logger.info("Đã warmup prompt cố định của SLM từ xa")
            except Exception:
                logger.exception("Warmup SLM từ xa chưa thành công; app vẫn nhận request")
        yield
    finally:
        metrics.stop_gpu_sampler()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await runtime.aclose()
        active_runtime.reset(token)


class RuntimeBindingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        application = scope["app"]
        if not hasattr(application.state, "runtime"):
            application.state.runtime = application.state.runtime_factory(application.state.settings)
        token = active_runtime.set(application.state.runtime)
        try:
            await self.app(scope, receive, send)
        finally:
            active_runtime.reset(token)


def create_app(*, config=None, runtime=None, runtime_factory=build_runtime) -> FastAPI:
    application = FastAPI(
        title="ViVi Local API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = config or get_settings()
    application.state.runtime_factory = runtime_factory
    if runtime is not None:
        application.state.runtime = runtime
    application.add_middleware(RuntimeBindingMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in application.state.settings.cors_origins.split(",") if origin.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (
        core.router,
        health.router,
        memory.router,
        history.router,
        metrics.router,
        vehicle.router,
        navigation.router,
        turns.router,
        speech.router,
        roles.router,
        engineer.router,
        web.router,  # last: it has a catch-all /{asset_name} route
    ):
        application.include_router(router)
    return application


app = create_app()
