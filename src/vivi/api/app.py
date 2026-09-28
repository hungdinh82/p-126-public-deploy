from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.vivi.api.routes import health, speech, turns, vehicle, web
from src.vivi.api.runtime import runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    config = runtime.settings
    if config.tts_provider != "off" and config.zerotts_preload:
        await runtime.tts.preload()
    if config.stt_provider != "off" and config.phowhisper_preload:
        await runtime.stt.preload()
    yield
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
    for router in (health.router, vehicle.router, turns.router, speech.router, web.router):
        application.include_router(router)
    return application


app = create_app()
