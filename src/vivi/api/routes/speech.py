from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from src.vivi.api.runtime import runtime
from src.vivi.domain.models import STTResponse, TTSRequest
from src.vivi.text import spoken_text

router = APIRouter(prefix="/api/v1")
_AUDIO_SUFFIXES = {".wav", ".webm", ".ogg", ".mp3", ".m4a"}


def _stt_identity() -> dict:
    engine = getattr(runtime.stt, "served_engine", None) or runtime.stt
    provider = getattr(engine, "name", runtime.stt.name)
    requested = getattr(engine, "model", None)
    if provider == "phowhisper":
        requested = runtime.settings.phowhisper_model
    elif provider == "jetson":
        requested = None
    elif provider == "whisper.cpp":
        requested = str(getattr(engine, "model", "")) or None
    served = getattr(engine, "served_model", None)
    device = getattr(engine, "device", "")
    location = "cloud" if device == "cloud" else "jetson" if provider == "jetson" else "laptop"
    if served is None and location == "laptop" and requested:
        served = str(requested)
    return {
        "provider": provider,
        "location": location,
        "requested_model": str(requested) if requested else None,
        "served_model": str(served) if served else None,
    }


def _tts_identity(tts) -> dict:
    requested = getattr(tts, "model_id", None)
    served = getattr(tts, "served_model", None)
    device = getattr(tts, "device", "")
    location = "cloud" if device == "cloud" else "jetson" if device == "jetson" else "laptop"
    if served is None and location == "laptop" and requested:
        served = requested
    return {
        "provider": tts.name,
        "location": location,
        "requested_model": requested,
        "served_model": served,
        "voice": getattr(tts, "voice", None),
    }


def _temporary_audio(content: bytes, suffix: str) -> Path:
    safe_suffix = suffix.lower() if suffix.lower() in _AUDIO_SUFFIXES else ".bin"
    with tempfile.NamedTemporaryFile(prefix="vivi-upload-", suffix=safe_suffix, delete=False) as handle:
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
    runtime.store.start_ai_route(session_id, turn_id, transcript="Đầu vào giọng nói")
    runtime.store.record_ai_stage(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "stage": "stt",
            "status": "calling",
            "provider": "pending",
            "requested_model": None,
            "served_model": None,
        }
    )
    try:
        transcript = await runtime.stt.transcribe(path)
    except Exception as exc:
        runtime.store.record_ai_stage(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "stage": "stt",
                "status": "error",
                "provider": "unknown",
                "requested_model": None,
                "served_model": None,
                "reason": type(exc).__name__,
            }
        )
        raise HTTPException(status_code=503, detail=f"STT chưa sẵn sàng: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    if not transcript:
        raise HTTPException(status_code=422, detail="Không nhận diện được lời nói")
    latency = round((time.perf_counter() - started) * 1000, 2)
    runtime.store.record_ai_stage(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "stage": "stt",
            "status": "served",
            "latency_ms": latency,
            **_stt_identity(),
        }
    )
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
    runtime.store.start_ai_route(session_id, turn_id, transcript="Đầu vào giọng nói trực tiếp")
    runtime.store.record_ai_stage(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "stage": "stt",
            "status": "calling",
            "provider": "pending",
            "requested_model": None,
            "served_model": None,
        }
    )

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
        try:
            await socket.send_json({"type": "error", "detail": "Không nhận diện được lời nói"})
            await socket.close()
        except Exception:
            pass  # The browser closed it first, e.g. a follow-up window nobody spoke in.
        return
    stored_path = runtime.store.save_audio(session_id, turn_id, ".webm", b"".join(received))
    runtime.store.record_ai_stage(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "stage": "stt",
            "status": "served",
            "latency_ms": latency,
            **_stt_identity(),
        }
    )
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


def _tts_candidates(request: TTSRequest):
    if request.tts_provider is None and runtime.tts.name == "off":
        raise HTTPException(status_code=503, detail="TTS đang tắt")
    preference = (
        runtime.tts.name
        if request.tts_provider is None
        else (None if request.tts_provider == "auto" else request.tts_provider)
    )
    if preference is not None and preference not in runtime.tts_engines:
        raise HTTPException(status_code=400, detail="TTS provider không khả dụng trong profile này")
    order = (
        ["vieneu", "zerotts"] if preference is None else [preference, "zerotts" if preference == "vieneu" else "vieneu"]
    )
    candidates = []
    for name in order:
        engine = (
            runtime.tts if request.tts_provider is None and name == runtime.tts.name else runtime.tts_engines.get(name)
        )
        location = "cloud" if name == "vieneu" else "jetson" if getattr(engine, "device", "") == "jetson" else "device"
        if not runtime.connectivity.capability_available("tts", location):
            continue
        if engine is not None and engine.availability()[0]:
            runtime.connectivity.store.append(
                "selection",
                "tts",
                "selected",
                provider=name,
                reason=f"preference={preference or 'auto'}",
            )
            candidates.append(engine)
    if candidates:
        return candidates
    raise HTTPException(status_code=503, detail="Không có TTS Cloud hoặc Jetson khả dụng; dùng giọng trình duyệt")


def _tts(request: TTSRequest):
    return _tts_candidates(request)[0]


@router.post("/tts")
async def synthesize(request: TTSRequest):
    for tts in _tts_candidates(request):
        runtime.store.record_ai_stage(
            {
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "stage": "tts",
                "status": "calling",
                **_tts_identity(tts),
            }
        )
        try:
            audio = await tts.synthesize(spoken_text(request.text))
            if not audio:
                raise ValueError("Empty TTS audio")
            runtime.store.record_ai_stage(
                {
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                    "stage": "tts",
                    "status": "served",
                    **_tts_identity(tts),
                }
            )
            return Response(audio, media_type="audio/wav", headers={"X-ViVi-Voice": quote(tts.voice)})
        except Exception as exc:
            runtime.store.record_ai_stage(
                {
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                    "stage": "tts",
                    "status": "fallback",
                    "reason": type(exc).__name__,
                    **_tts_identity(tts),
                }
            )
            runtime.connectivity.store.append(
                "provider_result", "tts", "fallback", provider=tts.name, reason=type(exc).__name__
            )
    raise HTTPException(status_code=503, detail="TTS chưa sẵn sàng; dùng giọng trình duyệt")


async def _close_audio(iterator):
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


@router.post("/tts/stream")
async def synthesize_stream(request: TTSRequest):
    started, received_at = time.perf_counter(), time.time()
    iterator = None
    for tts in _tts_candidates(request):
        runtime.store.record_ai_stage(
            {
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "stage": "tts",
                "status": "calling",
                **_tts_identity(tts),
            }
        )
        try:
            ensure_loaded = getattr(tts, "ensure_loaded", None)
            if ensure_loaded is not None:
                await ensure_loaded()
            iterator = tts.stream(spoken_text(request.text)).__aiter__()
            async with asyncio.timeout(
                runtime.settings.ai_server_request_timeout_seconds
                if tts.device == "jetson"
                else runtime.settings.vieneu_timeout_seconds
            ):
                first = await anext(iterator)
                while not first:
                    first = await anext(iterator)
            runtime.store.record_ai_stage(
                {
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                    "stage": "tts",
                    "status": "served",
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    **_tts_identity(tts),
                }
            )
            break
        except BaseException as exc:
            if iterator is not None:
                await _close_audio(iterator)
                iterator = None
            if not isinstance(exc, Exception):
                raise
            runtime.store.record_ai_stage(
                {
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                    "stage": "tts",
                    "status": "fallback",
                    "reason": type(exc).__name__,
                    **_tts_identity(tts),
                }
            )
            runtime.connectivity.store.append(
                "provider_result", "tts", "fallback", provider=tts.name, reason=type(exc).__name__
            )
    else:
        raise HTTPException(status_code=503, detail="TTS chưa sẵn sàng; dùng giọng trình duyệt")
    first_chunk = round((time.perf_counter() - started) * 1000, 2)
    closed = False

    async def cleanup():
        nonlocal closed
        if not closed:
            closed = True
            await _close_audio(iterator)

    async def timed_stream():
        audio_duration_ms = len(first) / 2 / tts.sample_rate * 1000
        try:
            yield first
            async for chunk in iterator:
                audio_duration_ms += len(chunk) / 2 / tts.sample_rate * 1000
                yield chunk
            runtime.store.record_tts(
                request.session_id,
                request.turn_id,
                {
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                    "received_at": received_at,
                    "text": request.text,
                    "time_to_first_chunk_ms": first_chunk,
                    "synthesis_ms": round((time.perf_counter() - started) * 1000, 2),
                    "audio_duration_ms": round(audio_duration_ms, 2),
                },
            )
        finally:
            await cleanup()

    # No server failover after PCM is emitted. The browser may retry this
    # sentence only; inference, retrieval and vehicle effects are never replayed.
    return StreamingResponse(
        timed_stream(),
        media_type="application/octet-stream",
        headers={
            "X-ViVi-Voice": quote(tts.voice),
            "X-ViVi-Audio-Format": "pcm_s16le",
            "X-ViVi-Sample-Rate": str(tts.sample_rate),
            "Cache-Control": "no-store",
        },
        background=BackgroundTask(cleanup),
    )
