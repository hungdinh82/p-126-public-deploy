from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket
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
    runtime.store.mark_input_start(session_id, turn_id, started, time.time())
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
            "event_type": "stt",
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


@router.websocket("/stt/stream")
async def transcribe_stream(socket: WebSocket, session_id: str, turn_id: str):
    """Live STT: the browser sends audio chunks and a final "stop" text frame.

    Replies are JSON: ``partial`` (final + interim text while speaking), then
    one ``done`` with the transcript, or ``error``.
    """
    await socket.accept()
    input_started = time.perf_counter()
    input_received_at = time.time()
    stream = getattr(runtime.stt, "stream", None)
    available, reason = runtime.stt.availability()
    if getattr(runtime.stt, "streaming", True) is False:
        stream = None
    if stream is None or not available:
        detail = reason if not available else f"{runtime.stt.name} không hỗ trợ streaming"
        await socket.send_json({"type": "error", "detail": f"STT chưa sẵn sàng: {detail}"})
        await socket.close()
        return
    runtime.store.mark_input_start(session_id, turn_id, input_started, input_received_at)

    received: list[bytes] = []
    # Latency is measured from the end of speech, not from the first chunk.
    stopped_at: list[float] = []

    async def audio_chunks():
        while True:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data:
                received.append(data)
                yield data
            elif message.get("text") == "stop":
                stopped_at.append(time.perf_counter())
                return

    async def on_update(final: str, interim: str) -> None:
        await socket.send_json({"type": "partial", "final": final, "interim": interim})

    try:
        transcript = await asyncio.wait_for(
            stream(audio_chunks(), on_update),
            timeout=getattr(runtime.stt, "stream_max_seconds", 60),
        )
    except Exception as exc:
        try:
            await socket.send_json({"type": "error", "detail": f"STT lỗi: {exc}"})
            await socket.close()
        except Exception:
            pass  # The browser already left.
        return
    latency = round((time.perf_counter() - stopped_at[0]) * 1000, 2) if stopped_at else None
    if not transcript:
        await socket.send_json({"type": "error", "detail": "Không nhận diện được lời nói"})
        await socket.close()
        return
    stored_path = runtime.store.save_audio(session_id, turn_id, ".webm", b"".join(received))
    audio_path = str(stored_path) if stored_path is not None else None
    runtime.store.append_event(
        {
            "event_type": "stt",
            "type": "stt",
            "session_id": session_id,
            "turn_id": turn_id,
            "transcript": transcript,
            "audio_path": audio_path,
            "latency_ms": latency,
            "streaming": True,
        }
    )
    await socket.send_json(
        {
            "type": "done",
            "transcript": transcript,
            "provider": runtime.stt.name,
            "audio_path": audio_path,
            "latency_ms": latency,
        }
    )
    await socket.close()


def _tts(request: TTSRequest):
    if request.tts_provider is None:
        return runtime.tts
    return runtime.tts_engines[request.tts_provider]


@router.post("/tts")
async def synthesize(request: TTSRequest):
    tts = _tts(request)
    try:
        audio = await tts.synthesize(request.text)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {exc}") from exc
    return Response(
        audio,
        media_type="audio/wav",
        headers={"X-ViVi-Voice": quote(tts.voice)},
    )


@router.post("/tts/stream")
async def synthesize_stream(request: TTSRequest):
    tts = _tts(request)
    available, reason = tts.availability()
    if not available:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {reason}")
    try:
        ensure_loaded = getattr(tts, "ensure_loaded", None)
        if ensure_loaded is not None:
            await ensure_loaded()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TTS chưa sẵn sàng: {exc}") from exc
    async def timed_stream():
        started = time.perf_counter()
        received_at = time.time()
        first_chunk = None
        audio_duration_ms = 0.0
        async for chunk in tts.stream(request.text):
            if first_chunk is None:
                first_chunk = round((time.perf_counter() - started) * 1000, 2)
            audio_duration_ms += len(chunk) / 2 / tts.sample_rate * 1000
            yield chunk
        runtime.store.record_tts(request.session_id, request.turn_id, {
            "session_id": request.session_id,
            "turn_id": request.turn_id,
            "received_at": received_at,
            "text": request.text,
            "time_to_first_chunk_ms": first_chunk,
            "synthesis_ms": round((time.perf_counter() - started) * 1000, 2),
            "audio_duration_ms": round(audio_duration_ms, 2),
        })

    return StreamingResponse(
        timed_stream(),
        media_type="application/octet-stream",
        headers={
            # Voice names are Vietnamese; headers only carry latin-1.
            "X-ViVi-Voice": quote(tts.voice),
            "X-ViVi-Audio-Format": "pcm_s16le",
            "X-ViVi-Sample-Rate": str(tts.sample_rate),
            "Cache-Control": "no-store",
        },
    )
