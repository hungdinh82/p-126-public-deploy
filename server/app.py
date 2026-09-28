from __future__ import annotations

import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from src.vivi.config import settings

from .adapters.stt import create_stt
from .adapters.tts import create_tts
from .data_store import DataStore
from .langgraph_orchestrator import create_langgraph_orchestrator
from .schemas import (
    ConfirmationDecisionRequest,
    DemoDrivingRequest,
    STTResponse,
    TTSRequest,
    TurnRequest,
    TurnResponse,
    VehicleState,
)
from .vehicle import VehicleSimulator
from .vehicle_mqtt import MqttVehicleAdapter

ROOT = Path(__file__).resolve().parent.parent
store = DataStore(settings)
LLM_MODELS = {
    "rules": "Kịch bản + LangGraph",
    "openai": settings.openai_model,
    "google": settings.google_model,
    "local": settings.local_llm_model,
}
if settings.vehicle_provider == "mqtt":
    vehicle = MqttVehicleAdapter(
        settings.vehicle_id,
        host=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_api_username or None,
        password=settings.mqtt_api_password or None,
        timeout=settings.mqtt_timeout_seconds,
    )
else:
    vehicle = VehicleSimulator()
orchestrator = create_langgraph_orchestrator(vehicle, store, settings)
stt = create_stt(settings)
tts = create_tts(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.tts_provider != "off" and settings.zerotts_preload:
        await tts.preload()
    if settings.stt_provider != "off" and settings.phowhisper_preload:
        await stt.preload()
    yield
    if isinstance(vehicle, MqttVehicleAdapter):
        vehicle.close()


app = FastAPI(title="ViVi Local API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
async def health():
    stt_ok, stt_detail = stt.availability()
    tts_ok, tts_detail = tts.availability()
    llm_options = []
    for provider, model in LLM_MODELS.items():
        available = provider in orchestrator.graph_providers
        detail = orchestrator.provider_errors.get(provider)
        if provider == "local" and available:
            headers = (
                {"Authorization": f"Bearer {settings.local_llm_api_key}"}
                if settings.local_llm_api_key
                else {}
            )
            try:
                async with httpx.AsyncClient(timeout=0.75) as client:
                    response = await client.get(
                        settings.local_llm_base_url.rstrip("/") + "/models",
                        headers=headers,
                    )
                    response.raise_for_status()
            except Exception as exc:
                available = False
                detail = f"Local LLM endpoint chưa sẵn sàng: {exc}"
        llm_options.append(
            {
                "provider": provider,
                "model": model,
                "available": available,
                "detail": detail,
            }
        )
    active_provider = next(
        (
            item["provider"]
            for item in llm_options
            if item["provider"] == orchestrator.default_provider and item["available"]
        ),
        "rules",
    )
    return {
        "status": "ok",
        "runtime": "local",
        "orchestration": {
            "engine": "langgraph",
            "graph_providers": sorted(orchestrator.graph_providers),
            "retrieval": {
                "sqlite": "sqlite_fts5",
                "lexical": "lexical_bm25",
                "hybrid": "chroma_hybrid",
            }[settings.rag_retrieval_mode],
            "retrieval_mode": settings.rag_retrieval_mode,
            "handbook_available": settings.rag_handbook_db.is_file(),
            "handbook_path": str(settings.rag_handbook_db),
        },
        "llm": {
            "provider": active_provider,
            "configured": settings.llm_provider in orchestrator.graph_providers,
            "detail": orchestrator.provider_errors.get(settings.llm_provider),
            "options": llm_options,
        },
        "stt": {
            "provider": stt.name,
            "available": stt_ok,
            "detail": stt_detail,
            "device": stt.device,
            "dtype": stt.dtype,
        },
        "tts": {
            "provider": tts.name,
            "voice": settings.zerotts_voice,
            "available": tts_ok,
            "detail": tts_detail,
            "requested_device": settings.zerotts_device,
            "device": tts.device,
            "execution_providers": tts.execution_providers,
        },
        "storage": {"audio": settings.store_audio, "transcripts": settings.store_transcripts, "path": str(settings.data_dir)},
        "vehicle": {"provider": vehicle.name, "connected": vehicle.is_connected(), "vehicle_id": settings.vehicle_id},
    }


@app.get("/health", include_in_schema=False)
async def legacy_health():
    return await health()


@app.get("/api/v1/vehicle/state")
async def vehicle_state():
    try:
        return await vehicle.get_state("api-read")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Xe mô phỏng chưa sẵn sàng") from exc


@app.put("/api/v1/demo/vehicle/driving", response_model=VehicleState)
async def set_demo_driving(request: DemoDrivingRequest):
    if not isinstance(vehicle, VehicleSimulator):
        raise HTTPException(
            status_code=409,
            detail="Chế độ lái demo chỉ khả dụng với VIVI_VEHICLE_PROVIDER=memory.",
        )
    try:
        return vehicle.set_driving(request.session_id, request.driving)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/turn", response_model=TurnResponse)
async def turn(request: TurnRequest):
    if request.confirmation_id or request.confirmation_decision:
        raise HTTPException(
            status_code=409,
            detail="Dùng POST /api/v1/confirmations/{id} để xử lý xác nhận.",
        )
    selected = request.llm_provider or orchestrator.default_provider
    if selected not in orchestrator.graph_providers:
        raise HTTPException(
            status_code=400,
            detail=orchestrator.provider_errors.get(selected, "LLM provider không khả dụng"),
        )
    try:
        return await orchestrator.run(request, provider=selected)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không xử lý được lượt hội thoại: {exc}") from exc


@app.post("/api/v1/turn/stream")
async def turn_stream(request: TurnRequest):
    if request.confirmation_id or request.confirmation_decision:
        raise HTTPException(
            status_code=409,
            detail="Dùng POST /api/v1/confirmations/{id} để xử lý xác nhận.",
        )
    selected = request.llm_provider or orchestrator.default_provider
    if selected not in orchestrator.graph_providers:
        raise HTTPException(
            status_code=400,
            detail=orchestrator.provider_errors.get(selected, "LLM provider không khả dụng"),
        )

    async def events():
        try:
            async for event in orchestrator.run_stream(request, provider=selected):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "detail": f"Không xử lý được lượt hội thoại: {exc}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@app.post("/api/v1/confirmations/{confirmation_id}", response_model=TurnResponse)
async def decide_confirmation(confirmation_id: str, request: ConfirmationDecisionRequest):
    selected = request.llm_provider or orchestrator.default_provider
    if selected not in orchestrator.graph_providers:
        raise HTTPException(
            status_code=400,
            detail=orchestrator.provider_errors.get(selected, "LLM provider không khả dụng"),
        )
    turn_request = TurnRequest(
        transcript="Xác nhận" if request.decision == "approve" else "Hủy",
        session_id=request.session_id,
        turn_id=request.turn_id,
        confirmation_id=confirmation_id,
        confirmation_decision=request.decision,
        llm_provider=request.llm_provider,
    )
    try:
        return await orchestrator.run(turn_request, provider=selected)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không xử lý được xác nhận: {exc}") from exc


@app.post("/api/v1/stt", response_model=STTResponse)
async def transcribe(
    audio: UploadFile = File(...),
    session_id: str = Form(...),
    turn_id: str = Form(...),
):
    content = await audio.read()
    if not content or len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Audio rỗng hoặc vượt quá 25 MB")
    suffix = Path(audio.filename or "audio.webm").suffix
    stored_path = store.save_audio(session_id, turn_id, suffix, content)
    temporary_path: Path | None = None
    if stored_path is None:
        safe_suffix = suffix.lower() if suffix.lower() in {".wav", ".webm", ".ogg", ".mp3", ".m4a"} else ".bin"
        with tempfile.NamedTemporaryFile(prefix="vivi-upload-", suffix=safe_suffix, delete=False) as handle:
            handle.write(content)
            temporary_path = Path(handle.name)
    path = stored_path or temporary_path
    assert path is not None
    started = time.perf_counter()
    try:
        transcript = await stt.transcribe(path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"STT chưa sẵn sàng: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if not transcript:
        raise HTTPException(status_code=422, detail="Không nhận diện được lời nói")
    latency = round((time.perf_counter() - started) * 1000, 2)
    audio_path = str(stored_path) if stored_path is not None else None
    store.append_event({"type": "stt", "session_id": session_id, "turn_id": turn_id, "transcript": transcript, "audio_path": audio_path, "latency_ms": latency})
    return STTResponse(session_id=session_id, turn_id=turn_id, transcript=transcript, provider=stt.name, audio_path=audio_path, latency_ms=latency)


@app.post("/api/v1/tts")
async def synthesize(request: TTSRequest):
    try:
        audio = await tts.synthesize(request.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {exc}") from exc
    return Response(audio, media_type="audio/wav", headers={"X-ViVi-Voice": settings.zerotts_voice})


@app.post("/api/v1/tts/stream")
async def synthesize_stream(request: TTSRequest):
    available, reason = tts.availability()
    if not available:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {reason}")
    try:
        ensure_loaded = getattr(tts, "ensure_loaded", None)
        if ensure_loaded is not None:
            await ensure_loaded()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {exc}") from exc
    return StreamingResponse(
        tts.stream(request.text),
        media_type="application/octet-stream",
        headers={
            "X-ViVi-Voice": settings.zerotts_voice,
            "X-ViVi-Audio-Format": "pcm_s16le",
            "X-ViVi-Sample-Rate": str(tts._model.sample_rate),
            "Cache-Control": "no-store",
        },
    )


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/{asset_name}")
async def asset(asset_name: str):
    if asset_name not in {"app.js", "style.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(ROOT / asset_name, headers={"Cache-Control": "no-store"})
