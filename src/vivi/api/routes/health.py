from __future__ import annotations

import httpx
from fastapi import APIRouter

from src.vivi.api.runtime import runtime

router = APIRouter()


async def _local_llm_available() -> tuple[bool, str | None]:
    config = runtime.settings
    headers = {"Authorization": f"Bearer {config.local_llm_api_key}"} if config.local_llm_api_key else {}
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
        available = provider in getattr(
            runtime.orchestrator, "semantic_providers", runtime.orchestrator.graph_providers
        )
        detail = runtime.orchestrator.provider_errors.get(provider)
        if not runtime.connectivity.provider_allowed(provider):
            available = False
            detail = "Không khả dụng theo trạng thái kết nối/profile hiện tại"
        elif provider == "local" and available:
            available, detail = await _local_llm_available()
        options.append(
            {"provider": provider, "model": model, "available": available, "fallback_available": True, "detail": detail}
        )
    return options


def _fallback_chain(config):
    return runtime.orchestrator.graph.navigation.workflow.resolver.gateway.chain(config.llm_provider)


@router.get("/api/v1/health")
async def health():
    config = runtime.settings
    connectivity = runtime.connectivity.snapshot()
    stt_ok, stt_detail = runtime.stt.availability()
    tts_ok, tts_detail = runtime.tts.availability()
    llm_options = await _llm_options()
    active_provider = runtime.orchestrator.default_provider
    tts_options = []
    for name, engine in runtime.tts_engines.items():
        ok, detail = engine.availability()
        location = "cloud" if name == "vieneu" else "jetson" if engine.device == "jetson" else "device"
        if not runtime.connectivity.capability_available("tts", location):
            ok, detail = False, "Không khả dụng theo trạng thái kết nối/profile hiện tại"
        tts_options.append(
            {
                "provider": name,
                "voice": engine.voice,
                "device": engine.device,
                "available": ok,
                "detail": detail,
            }
        )
    selected_tts = next((item for item in tts_options if item["provider"] == runtime.tts.name), None)
    if selected_tts is not None:
        tts_ok, tts_detail = selected_tts["available"], selected_tts["detail"]
    return {
        "status": "ok",
        "connectivity": connectivity,
        "runtime": "local",
        "orchestration": {
            "engine": "langgraph",
            "workflow_version": "core-2",
            "fallback_chain": _fallback_chain(config),
            "cloud_budget_seconds": config.cloud_budget_seconds,
            "local_budget_seconds": config.local_budget_seconds,
            "turn_timeout_seconds": config.turn_timeout_seconds,
            "durable_tasks": True,
            "graph_providers": sorted(runtime.orchestrator.graph_providers),
            "retrieval": {
                "sqlite": "sqlite_fts5",
                "sqlite_local": "sqlite_fts5_local_e5",
                "lexical": "lexical_bm25",
            }[config.rag_retrieval_mode],
            "retrieval_mode": config.rag_retrieval_mode,
            "handbook_available": config.rag_handbook_db.is_file(),
            "handbook_path": str(config.rag_handbook_db),
            "local_only": config.rag_local_only or config.jetson_ai_policy == "offline_only",
        },
        "llm": {
            "provider": active_provider,
            "configured": config.llm_provider
            in getattr(runtime.orchestrator, "semantic_providers", runtime.orchestrator.graph_providers),
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
            "voice": runtime.tts.voice,
            "available": tts_ok,
            "detail": tts_detail,
            "requested_device": config.zerotts_device,
            "device": runtime.tts.device,
            "execution_providers": runtime.tts.execution_providers,
            "options": [
                {
                    "provider": "auto",
                    "voice": "Tự động",
                    "device": "adaptive",
                    "available": any(item["available"] for item in tts_options),
                    "detail": "Tự chọn VieNeu → ZeroTTS → giọng trình duyệt",
                },
                *tts_options,
            ],
        },
        "storage": {
            "audio": config.store_audio,
            "transcripts": config.store_transcripts,
            "path": str(config.data_dir),
        },
        "memory": {
            "short_term_turns": config.rag_history_turns,
            "long_term_enabled": config.memory_enabled,
            "profile_id": config.memory_profile_id,
            "path": str(config.memory_path),
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


@router.get("/api/v1/connectivity")
async def connectivity():
    """Read background observations without probing any model or speech engine."""
    return runtime.connectivity.snapshot()
