from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.vivi.api.routes import health, metrics, navigation, speech, turns, vehicle, web
from src.vivi.api.runtime import runtime

logger = logging.getLogger(__name__)


async def _preload_stt_fallback() -> None:
    try:
        await runtime.stt.preload_fallback()
        logger.info("STT dự phòng đã tải xong")
    except Exception:
        logger.exception("Không tải được STT dự phòng")


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
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
        yield
    finally:
        metrics.stop_gpu_sampler()
        for task in background:
            task.cancel()
        runtime.close()


def create_app() -> FastAPI:
    application = FastAPI(
        title="ViVi Local API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in runtime.settings.cors_origins.split(",")
            if origin.strip()
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (
        health.router,
        metrics.router,
        vehicle.router,
        navigation.router,
        turns.router,
        speech.router,
        web.router,
    ):
        application.include_router(router)
    return application


app = create_app()
