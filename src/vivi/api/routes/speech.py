from __future__ import annotations

import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

from src.vivi.api.runtime import runtime
from src.vivi.domain.models import STTResponse, TTSRequest

router = APIRouter(prefix="/api/v1")
_AUDIO_SUFFIXES = {".wav", ".webm", ".ogg", ".mp3", ".m4a"}


def _temporary_audio(content: bytes, suffix: str) -> Path:
    safe_suffix = suffix.lower() if suffix.lower() in _AUDIO_SUFFIXES else ".bin"
    with tempfile.NamedTemporaryFile(
        prefix="vivi-upload-", suffix=safe_suffix, delete=False
    ) as handle:
        handle.write(content)
        return Path(handle.name)


@router.post("/stt", response_model=STTResponse)
async def transcribe(
    audio: UploadFile = File(...),
    session_id: str = Form(...),
    turn_id: str = Form(...),
):
    content = await audio.read()
    if not content or len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Audio rỗng hoặc vượt quá 25 MB")
    suffix = Path(audio.filename or "audio.webm").suffix
    stored_path = runtime.store.save_audio(session_id, turn_id, suffix, content)
    temporary_path = None if stored_path else _temporary_audio(content, suffix)
    path = stored_path or temporary_path
    assert path is not None
    started = time.perf_counter()
    try:
        transcript = await runtime.stt.transcribe(path)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"STT chưa sẵn sàng: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if not transcript:
        raise HTTPException(status_code=422, detail="Không nhận diện được lời nói")
    latency = round((time.perf_counter() - started) * 1000, 2)
    audio_path = str(stored_path) if stored_path is not None else None
    runtime.store.append_event(
        {
            "type": "stt",
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript": transcript,
            "audio_path": audio_path,
            "latency_ms": latency,
        }
    )
    return STTResponse(
        session_id=session_id,
        turn_id=turn_id,
        transcript=transcript,
        provider=runtime.stt.name,
        audio_path=audio_path,
        latency_ms=latency,
    )


@router.post("/tts")
async def synthesize(request: TTSRequest):
    try:
        audio = await runtime.tts.synthesize(request.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {exc}") from exc
    return Response(
        audio,
        media_type="audio/wav",
        headers={"X-ViVi-Voice": runtime.settings.zerotts_voice},
    )


@router.post("/tts/stream")
async def synthesize_stream(request: TTSRequest):
    available, reason = runtime.tts.availability()
    if not available:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {reason}")
    try:
        ensure_loaded = getattr(runtime.tts, "ensure_loaded", None)
        if ensure_loaded is not None:
            await ensure_loaded()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {exc}") from exc
    return StreamingResponse(
        runtime.tts.stream(request.text),
        media_type="application/octet-stream",
        headers={
            "X-ViVi-Voice": runtime.settings.zerotts_voice,
            "X-ViVi-Audio-Format": "pcm_s16le",
            "X-ViVi-Sample-Rate": str(runtime.tts._model.sample_rate),
            "Cache-Control": "no-store",
        },
    )
