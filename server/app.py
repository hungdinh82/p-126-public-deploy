from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from .adapters.llm import create_llm
from .adapters.stt import PhoWhisperAdapter
from .adapters.tts import ZeroTTSAdapter
from .config import settings
from .data_store import DataStore
from .langgraph_orchestrator import create_langgraph_orchestrator
from .orchestrator import Orchestrator
from .schemas import STTResponse, TTSRequest, TurnRequest, TurnResponse
from .vehicle import VehicleSimulator


ROOT = Path(__file__).resolve().parent.parent
store = DataStore(settings)
LLM_MODELS = {
    "rules": "Kịch bản + LangGraph",
    "openai": settings.openai_model,
    "google": settings.rag_generation_model,
    "local": settings.local_llm_model,
}
llm_adapters = {}
llm_errors = {}
for provider in LLM_MODELS:
    try:
        llm_adapters[provider] = create_llm(replace(settings, llm_provider=provider))
    except Exception as exc:
        llm_errors[provider] = str(exc)
llm = llm_adapters.get(settings.llm_provider, llm_adapters["rules"])
llm_error = llm_errors.get(settings.llm_provider) or (f"LLM_PROVIDER không hợp lệ: {settings.llm_provider}" if settings.llm_provider not in LLM_MODELS else None)
vehicle = VehicleSimulator()
legacy_orchestrator = Orchestrator(llm, vehicle, store)
orchestrator = create_langgraph_orchestrator(legacy_orchestrator, vehicle, store)
stt = PhoWhisperAdapter(settings)
tts = ZeroTTSAdapter(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tts.preload()
    if settings.phowhisper_preload:
        await stt.preload()
    yield


app = FastAPI(title="ViVi Local API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8787", "http://localhost:8787"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
async def health():
    stt_ok, stt_detail = stt.availability()
    tts_ok, tts_detail = tts.availability()
    return {
        "status": "ok",
        "runtime": "local",
        "orchestration": {
            "engine": "langgraph",
            "graph_providers": sorted(orchestrator.graph_providers),
            "retrieval": "lexical",
        },
        "llm": {
            "provider": llm.name, "configured": llm_error is None, "detail": llm_error,
            "options": [
                {"provider": provider, "model": model, "available": provider in llm_adapters, "detail": llm_errors.get(provider)}
                for provider, model in LLM_MODELS.items()
            ],
        },
        "stt": {
            "provider": stt.name,
            "available": stt_ok,
            "detail": stt_detail,
            "device": stt.device,
            "dtype": stt.dtype,
        },
        "tts": {"provider": tts.name, "voice": settings.zerotts_voice, "available": tts_ok, "detail": tts_detail},
        "storage": {"audio": settings.store_audio, "transcripts": settings.store_transcripts, "path": str(settings.data_dir)},
    }


@app.post("/api/v1/turn", response_model=TurnResponse)
async def turn(request: TurnRequest):
    selected = request.llm_provider or llm.name
    adapter = llm_adapters.get(selected)
    if adapter is None:
        raise HTTPException(status_code=400, detail=llm_errors.get(selected, "LLM provider không khả dụng"))
    try:
        return await orchestrator.run(request, llm=adapter)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không xử lý được lượt hội thoại: {exc}") from exc


@app.post("/api/v1/turn/stream")
async def turn_stream(request: TurnRequest):
    selected = request.llm_provider or llm.name
    adapter = llm_adapters.get(selected)
    if adapter is None:
        raise HTTPException(status_code=400, detail=llm_errors.get(selected, "LLM provider không khả dụng"))

    async def events():
        try:
            async for event in orchestrator.run_stream(request, llm=adapter):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:
            yield json.dumps({"type": "error", "detail": f"Không xử lý được lượt hội thoại: {exc}"}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


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
    path = store.save_audio(session_id, turn_id, suffix, content)
    if path is None:
        temp_path = settings.data_dir / "tmp" / f"{turn_id}{suffix or '.webm'}"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(content)
        path = temp_path
    started = time.perf_counter()
    try:
        transcript = await stt.transcribe(path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"PhoWhisper chưa sẵn sàng: {exc}") from exc
    if not transcript:
        raise HTTPException(status_code=422, detail="Không nhận diện được lời nói")
    latency = round((time.perf_counter() - started) * 1000, 2)
    store.append_event({"type": "stt", "session_id": session_id, "turn_id": turn_id, "transcript": transcript, "audio_path": str(path), "latency_ms": latency})
    return STTResponse(session_id=session_id, turn_id=turn_id, transcript=transcript, provider=stt.name, audio_path=str(path), latency_ms=latency)


@app.post("/api/v1/tts")
async def synthesize(request: TTSRequest):
    try:
        audio = await tts.synthesize(request.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ZeroTTS chưa sẵn sàng: {exc}") from exc
    return Response(audio, media_type="audio/wav", headers={"X-ViVi-Voice": settings.zerotts_voice})


@app.post("/api/v1/tts/stream")
async def synthesize_stream(request: TTSRequest):
    available, reason = tts.availability()
    if not available:
        raise HTTPException(status_code=503, detail=f"ZeroTTS chưa sẵn sàng: {reason}")
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
    return FileResponse(ROOT / "index.html")


@app.get("/{asset_name}")
async def asset(asset_name: str):
    if asset_name not in {"app.js", "style.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(ROOT / asset_name)
