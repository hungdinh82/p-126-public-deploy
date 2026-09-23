from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from .adapters.llm import create_llm
from .adapters.stt import PhoWhisperAdapter
from .adapters.tts import ZeroTTSAdapter
from .config import settings
from .data_store import DataStore
from .orchestrator import Orchestrator
from .schemas import STTResponse, TTSRequest, TurnRequest, TurnResponse
from .vehicle import VehicleSimulator


ROOT = Path(__file__).resolve().parent.parent
store = DataStore(settings)
try:
    llm = create_llm(settings)
    llm_error = None
except Exception as exc:
    from .adapters.llm import RulesAdapter
    llm, llm_error = RulesAdapter(), str(exc)
vehicle = VehicleSimulator()
orchestrator = Orchestrator(llm, vehicle, store)
stt = PhoWhisperAdapter(settings)
tts = ZeroTTSAdapter(settings)

app = FastAPI(title="ViVi Local API", version="0.1.0")
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
        "llm": {"provider": llm.name, "configured": llm_error is None, "detail": llm_error},
        "stt": {"provider": stt.name, "available": stt_ok, "detail": stt_detail},
        "tts": {"provider": tts.name, "voice": settings.zerotts_voice, "available": tts_ok, "detail": tts_detail},
        "storage": {"audio": settings.store_audio, "transcripts": settings.store_transcripts, "path": str(settings.data_dir)},
    }


@app.post("/api/v1/turn", response_model=TurnResponse)
async def turn(request: TurnRequest):
    try:
        return await orchestrator.run(request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không xử lý được lượt hội thoại: {exc}") from exc


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


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html")


@app.get("/{asset_name}")
async def asset(asset_name: str):
    if asset_name not in {"app.js", "style.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(ROOT / asset_name)
