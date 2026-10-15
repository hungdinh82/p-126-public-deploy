from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.vivi.api.routes import (
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
    voice_inputs,
    web,
)
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
        if config.local_voice_buffering_enabled:
            from src.vivi.speech.input_service import VoiceInputService
            runtime.voice = VoiceInputService(runtime)
            await runtime.voice.start()
        await runtime.connectivity.start()
        if config.tts_provider != "off" and config.zerotts_preload:
            await runtime.tts.preload()
        if config.stt_provider != "off" and config.phowhisper_preload:
            await runtime.stt.preload()
        if config.stt_fallback_preload and hasattr(runtime.stt, "preload_fallback"):
            background.append(asyncio.create_task(_preload_stt_fallback()))
        # Local tool documentation is selected per input. A full-catalog warmup
        # primes a different prefix and can delay an actual turn or its repair.
        # The AI server already loads the model; let the first real request
        # prime its own documentation prefix instead of scheduling extra work.
        yield
    finally:
        metrics.stop_gpu_sampler()
        if runtime.voice is not None:
            await runtime.voice.close()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await runtime.aclose()


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
        memory.router,
        history.router,
        metrics.router,
        vehicle.router,
        navigation.router,
        turns.router,
        speech.router,
        voice_inputs.router,
        roles.router,
        engineer.router,
        web.router,  # last: it has a catch-all /{asset_name} route
    ):
        application.include_router(router)
    return application


app = create_app()
