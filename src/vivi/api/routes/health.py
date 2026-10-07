from __future__ import annotations

import httpx
from fastapi import APIRouter

from src.vivi.api.runtime import runtime

router = APIRouter()


async def _local_llm_available() -> tuple[bool, str | None]:
    config = runtime.settings
    headers = (
        {"Authorization": f"Bearer {config.local_llm_api_key}"}
        if config.local_llm_api_key
        else {}
    )
    try:
        async with httpx.AsyncClient(timeout=0.75) as client:
            response = await client.get(
                config.local_llm_base_url.rstrip("/") + "/models",
                headers=headers,
            )
            response.raise_for_status()
    except Exception as exc:
        return False, f"Local LLM endpoint chưa sẵn sàng: {exc}"
    return True, None


async def _llm_options() -> list[dict]:
    options = []
    for provider, model in runtime.llm_models.items():
        available = provider in runtime.orchestrator.graph_providers
        detail = runtime.orchestrator.provider_errors.get(provider)
        if provider == "local" and available:
            available, detail = await _local_llm_available()
        options.append(
            {"provider": provider, "model": model, "available": available, "detail": detail}
        )
    return options


@router.get("/api/v1/health")
async def health():
    config = runtime.settings
    stt_ok, stt_detail = runtime.stt.availability()
    tts_ok, tts_detail = runtime.tts.availability()
    llm_options = await _llm_options()
    active_provider = next(
        (
            item["provider"]
            for item in llm_options
            if item["provider"] == runtime.orchestrator.default_provider
            and item["available"]
        ),
        "rules",
    )
    return {
        "status": "ok",
        "runtime": "local",
        "orchestration": {
            "engine": "langgraph",
            "graph_providers": sorted(runtime.orchestrator.graph_providers),
            "retrieval": {
                "sqlite": "sqlite_fts5",
                "sqlite_local": "sqlite_fts5_local_e5",
                "lexical": "lexical_bm25",
            }[config.rag_retrieval_mode],
            "retrieval_mode": config.rag_retrieval_mode,
            "handbook_available": config.rag_handbook_db.is_file(),
            "handbook_path": str(config.rag_handbook_db),
            "local_only": config.rag_local_only,
        },
        "llm": {
            "provider": active_provider,
            "configured": config.llm_provider in runtime.orchestrator.graph_providers,
            "detail": runtime.orchestrator.provider_errors.get(config.llm_provider),
            "options": llm_options,
        },
        "stt": {
            "provider": runtime.stt.name,
            "available": stt_ok,
            "detail": stt_detail,
            "device": runtime.stt.device,
            "dtype": runtime.stt.dtype,
            "streaming": bool(getattr(runtime.stt, "streaming", hasattr(runtime.stt, "stream"))),
        },
        "tts": {
            "provider": runtime.tts.name,
            "voice": config.zerotts_voice,
            "available": tts_ok,
            "detail": tts_detail,
            "requested_device": config.zerotts_device,
            "device": runtime.tts.device,
            "execution_providers": runtime.tts.execution_providers,
        },
        "storage": {
            "audio": config.store_audio,
            "transcripts": config.store_transcripts,
            "path": str(config.data_dir),
        },
        "vehicle": {
            "provider": runtime.vehicle.name,
            "connected": runtime.vehicle.is_connected(),
            "vehicle_id": config.vehicle_id,
        },
    }


@router.get("/health", include_in_schema=False)
async def legacy_health():
    return await health()
