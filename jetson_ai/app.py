"""Stateless inference gateway. No ViVi workflow or vehicle dependencies."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from jetson_ai.config import auth_required
from jetson_ai.model_metrics import memory_snapshot
from jetson_ai.speech import SpeechModels
from jetson_ai.stream_routes import ModelStreams

VERSION = "1.0"


@dataclass(frozen=True)
class ServiceConfig:
    token: str
    model_id: str = "vivi-qwen3-1.7b"
    backend_url: str = "http://127.0.0.1:1235"
    backend_id: str = "jetson-xavier"
    max_input_bytes: int = 262144
    max_tokens: int = 2048
    timeout_seconds: float = 60.0
    max_concurrent: int = 1
    enable_speech: bool = False
    llm_model_path: str = ""
    llm_log_path: str = ""
    auth_required: bool = True

    @classmethod
    def from_env(cls) -> ServiceConfig:
        token = os.environ.get("AI_SERVER_TOKEN") or os.environ.get("JETSON_AI_TOKEN", "")
        required = auth_required()
        if required and len(token) < 32:
            raise RuntimeError("JETSON_AI_TOKEN must contain at least 32 characters")
        concurrency = int(os.environ.get("JETSON_AI_MAX_CONCURRENT", "1"))
        if concurrency < 1 or concurrency > 4:
            raise RuntimeError("JETSON_AI_MAX_CONCURRENT must be between 1 and 4")
        return cls(
            token=token,
            auth_required=required,
            model_id=os.environ.get("JETSON_AI_MODEL_ID", "vivi-qwen3-1.7b"),
            backend_url=os.environ.get("JETSON_AI_LLM_URL", "http://127.0.0.1:1235"),
            backend_id=os.environ.get("JETSON_AI_BACKEND_ID", "jetson-xavier"),
            max_concurrent=concurrency,
            enable_speech=os.environ.get("JETSON_AI_ENABLE_SPEECH") == "1",
            llm_model_path=os.environ.get("JETSON_AI_LLM_MODEL_PATH", ""),
            llm_log_path=os.environ.get("JETSON_AI_LLM_LOG", ""),
        )


def create_app(config: ServiceConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    slots = asyncio.Semaphore(config.max_concurrent)
    active = 0
    last_llm_latency = None
    last_llm_first_chunk = None
    speech = SpeechModels()
    load_task = None

    async def load_speech():
        async with slots:
            for name in ("stt", "tts"):
                if speech.info[name]["status"] != "ready":
                    await asyncio.to_thread(speech.load, name)

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal load_task
        if config.enable_speech:
            load_task = asyncio.create_task(load_speech())
        yield
        if load_task and not load_task.done():
            load_task.cancel()
            await asyncio.gather(load_task, return_exceptions=True)

    app = FastAPI(title="ViVi Jetson AI", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.speech = speech

    def error(code: str, message: str, status: int, *, request: Request | None = None,
              retryable: bool = False, capability: str = "llm") -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "retryable": retryable,
                               "backend_id": config.backend_id, "capability": capability,
                               "call_id": getattr(request.state, "call_id", request.headers.get("x-vivi-call-id", "")) if request else "",
                               "attempt": getattr(request.state, "attempt", request.headers.get("x-vivi-attempt", "1")) if request else "1"}},
        )

    async def backend_status() -> str:
        try:
            async with httpx.AsyncClient(transport=transport, timeout=2) as client:
                response = await client.get(config.backend_url.rstrip("/") + "/health")
            body = response.json()
            status = body.get("status", "") if isinstance(body, dict) else ""
            if response.status_code == 200 and status == "ok":
                return "ready"
            if str(status).startswith("loading"):
                return "loading"
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return "unavailable"

    def record_llm(latency, first_chunk):
        nonlocal last_llm_latency, last_llm_first_chunk
        last_llm_latency, last_llm_first_chunk = latency, first_chunk

    streams = ModelStreams(config, speech, slots, backend_status, error, record_llm, transport)
    streams.install(app)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path in {"/health/live", "/", "/test", "/streaming.js"}:
            return await call_next(request)
        supplied = request.headers.get("authorization", "")
        if config.auth_required and not hmac.compare_digest(supplied, f"Bearer {config.token}"):
            response = error("UNAUTHORIZED", "Invalid service token", 401, request=request)
        else:
            response = await call_next(request)
        response.headers["X-ViVi-Contract-Version"] = VERSION
        for name in ("X-ViVi-Request-Id", "X-ViVi-Call-Id", "X-ViVi-Attempt"):
            if name in request.headers:
                response.headers[name] = request.headers[name]
        return response

    @app.get("/health/live")
    async def live():
        return {"status": "live", "contract_version": VERSION}

    @app.get("/streaming.js", include_in_schema=False)
    async def streaming_script():
        return Response((Path(__file__).parent / "static/streaming.js").read_text(encoding="utf-8"),
                        media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/test", response_class=HTMLResponse, include_in_schema=False)
    async def test_ui(request: Request):
        html = (Path(__file__).parent / "static/test.html").read_text(encoding="utf-8")
        local = request.client and request.client.host in {"127.0.0.1", "::1"}
        trusted_host = request.url.hostname in {"127.0.0.1", "localhost", "::1"}
        # LAN clients enter their token. Never trust Host or proxy headers alone.
        bootstrap = json.dumps(config.token).replace("<", "\\u003c") if config.auth_required and local and trusted_host else '""'
        html = html.replace("__AUTH_REQUIRED_JSON__", json.dumps(config.auth_required))
        return HTMLResponse(html.replace("__LOCAL_TOKEN_JSON__", bootstrap),
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/models/load")
    async def reload_models():
        nonlocal load_task
        if load_task is None or load_task.done():
            load_task = asyncio.create_task(load_speech())
        return {"status": "loading"}

    @app.get("/v1/capabilities")
    async def capabilities():
        llm_status = await backend_status()
        if llm_status == "ready" and slots.locked():
            llm_status = "busy"
        log = Path(config.llm_log_path)
        data = log.read_text(errors="replace") if config.llm_log_path and log.is_file() else ""
        offloads = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", data)
        gpu = bool(llm_status in {"ready", "busy"} and offloads and int(offloads[-1][0]) > 0)
        devices = re.findall(r"Device \d+: ([^\n,]+)", data)
        result = {"contract_version": VERSION, "backend_id": config.backend_id,
                "host": socket.gethostname(), "api_url": config.backend_url,
                "capabilities": {
                    "llm": {"status": llm_status, "model_id": config.model_id,
                            "stream": True, "stream_protocol": "sse", "queue_depth": 0,
                            "model_path": config.llm_model_path, "api_url": config.backend_url,
                            "host": socket.gethostname(), "pid": os.environ.get("JETSON_AI_LLM_PID"),
                            "device": "not-loaded" if llm_status in {"unavailable", "loading"} else "cuda" if gpu else "cpu" if offloads else "unknown",
                            "using_gpu": gpu, "gpu_name": devices[0] if devices else None,
                            "gpu_layers": int(offloads[-1][0]) if offloads else None,
                            "load_ms": os.environ.get("JETSON_AI_LLM_LOAD_MS"),
                            "last_latency_ms": last_llm_latency,
                            "first_chunk_ms": last_llm_first_chunk,
                            "limits": {"max_input_bytes": config.max_input_bytes, "max_tokens": config.max_tokens}},
                    "stt": dict(speech.info["stt"]),
                    "tts": dict(speech.info["tts"]),
                }}
        models = result["capabilities"]
        architectures = re.findall(r"general\.architecture(?:\s+str)?\s*(?:=|:)\s*(\S+)", data)
        models["llm"]["architecture"] = architectures[-1] if architectures else config.model_id
        models["llm"]["engine"] = "llama.cpp"
        quant = re.search(r"(?:^|[-_.])(Q\d+_[A-Z0-9_]+|F16|BF16|F32)(?=\.gguf$)",
                          Path(config.llm_model_path).name, re.I)
        models["llm"]["precision"] = quant.group(1).upper() + " (GGUF weights)" if quant else None
        models["stt"]["architecture"] = "Whisper · encoder/decoder Transformer"
        models["tts"]["architecture"] = "ZeroTTS · text encoder / prefix / local decoder + audio codec"
        if speech.stt_engine == "whisper_cpp":
            models["stt"]["pid"] = os.environ.get("JETSON_AI_STT_PID")
        result["system_memory"] = memory_snapshot(models)
        return result

    @app.get("/v1/models")
    async def models(request: Request):
        if await backend_status() != "ready":
            return error("CAPABILITY_UNAVAILABLE", "LLM backend is not ready", 503,
                         request=request, retryable=True)
        return {"object": "list", "data": [{"id": config.model_id, "object": "model", "owned_by": config.backend_id}]}

    async def complete(payload: dict[str, Any], request: Request, timeout_ms: int | None = None):
        nonlocal active, last_llm_latency, last_llm_first_chunk
        state = await backend_status()
        if state != "ready":
            code = "MODEL_LOADING" if state == "loading" else "CAPABILITY_UNAVAILABLE"
            return error(code, "LLM backend is not ready", 503, request=request, retryable=True)
        if slots.locked():
            result = error("BACKEND_BUSY", "LLM backend is busy", 429, request=request, retryable=True)
            result.headers["Retry-After"] = "1"
            return result
        async with slots:
            active += 1
            started = time.monotonic()
            try:
                timeout = min(config.timeout_seconds, timeout_ms / 1000) if timeout_ms else config.timeout_seconds
                async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
                    response = await client.post(config.backend_url.rstrip("/") + "/v1/chat/completions", json=payload)
                if response.status_code >= 400:
                    code = "BACKEND_BUSY" if response.status_code == 429 else "MODEL_ERROR"
                    return error(code, "LLM backend rejected request", 429 if response.status_code == 429 else 502,
                                 request=request, retryable=response.status_code in {429, 503})
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or not body["choices"]:
                    raise ValueError("missing choices")
                message = body["choices"][0].get("message") if isinstance(body["choices"][0], dict) else None
                if not isinstance(message, dict) or ("content" not in message and "tool_calls" not in message):
                    raise ValueError("missing assistant message")
                return body
            except httpx.TimeoutException:
                return error("SERVICE_TIMEOUT", "LLM backend timed out", 504, request=request, retryable=True)
            except (httpx.HTTPError, ValueError, TypeError):
                return error("MODEL_ERROR", "LLM backend returned an invalid response", 502, request=request)
            finally:
                active -= 1
                last_llm_latency = round((time.monotonic() - started) * 1000)
                last_llm_first_chunk = None

    async def validate_json(request: Request) -> dict[str, Any] | JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > config.max_input_bytes:
                return error("INVALID_REQUEST", "Request too large", 413, request=request)
        try:
            import json
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return error("INVALID_REQUEST", "Invalid JSON", 400, request=request)
        if not isinstance(payload, dict):
            return error("INVALID_REQUEST", "Expected JSON object", 400, request=request)
        return payload

    def validate_messages(payload: dict[str, Any], request: Request) -> JSONResponse | None:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 32:
            return error("INVALID_REQUEST", "messages must contain 1..32 items", 400, request=request)
        if any(not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant", "tool"}
               or not isinstance(item.get("content"), (str, list, type(None))) for item in messages):
            return error("INVALID_REQUEST", "Invalid messages", 400, request=request)
        tokens = payload.get("max_tokens", 512)
        if type(tokens) is not int or not 1 <= tokens <= config.max_tokens:
            return error("INVALID_REQUEST", "Invalid max_tokens", 400, request=request)
        if type(payload.get("stream", False)) is not bool:
            return error("INVALID_REQUEST", "stream must be a boolean", 400, request=request)
        if payload.get("stream") and payload.get("n", 1) != 1:
            return error("INVALID_REQUEST", "Streaming supports n = 1", 400, request=request)
        payload.setdefault("max_tokens", 512)
        return None

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        payload = await validate_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        invalid = validate_messages(payload, request)
        if invalid:
            return invalid
        payload["model"] = config.model_id
        raw_timeout = request.headers.get("X-ViVi-Timeout-Ms")
        if raw_timeout is not None:
            try:
                timeout_ms = int(raw_timeout)
            except ValueError:
                return error("INVALID_REQUEST", "Invalid timeout_ms", 400, request=request)
            if timeout_ms < 1:
                return error("DEADLINE_EXCEEDED", "Deadline expired", 408, request=request)
        else:
            timeout_ms = None
        if payload.get("stream"):
            return await streams.chat(payload, request, timeout_ms)
        return await complete(payload, request, timeout_ms)

    @app.post("/v1/llm/generate")
    async def generate(request: Request):
        payload = await validate_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        if payload.get("contract_version") != VERSION or payload.get("capability") != "llm":
            return error("INVALID_REQUEST", "Unsupported contract version or capability", 400, request=request)
        if not all(isinstance(payload.get(field), str) and payload[field] for field in ("request_id", "call_id")):
            return error("INVALID_REQUEST", "Missing request_id or call_id", 400, request=request)
        if type(payload.get("attempt")) is not int or payload["attempt"] < 1:
            return error("INVALID_REQUEST", "Invalid attempt", 400, request=request)
        request.state.call_id = payload["call_id"]
        request.state.attempt = payload["attempt"]
        timeout_ms = payload.get("timeout_ms")
        if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms < 1):
            return error("DEADLINE_EXCEEDED", "Deadline expired", 408, request=request)
        source = payload.get("input")
        if not isinstance(source, dict):
            return error("INVALID_REQUEST", "Missing input", 400, request=request)
        source = {**source, "stream": payload.get("stream", source.get("stream", False))}
        invalid = validate_messages(source, request)
        if invalid:
            return invalid
        source = {**source, "model": config.model_id}
        if source.get("stream"):
            return await streams.chat(source, request, timeout_ms, {
                "request_id": payload["request_id"], "call_id": payload["call_id"], "attempt": payload["attempt"]})
        start = time.monotonic()
        result = await complete(source, request, timeout_ms)
        if isinstance(result, JSONResponse):
            return result
        message = result["choices"][0].get("message", {})
        return {"contract_version": VERSION, "request_id": payload.get("request_id", ""),
                "call_id": payload.get("call_id", ""), "attempt": payload.get("attempt", 1),
                "backend_id": config.backend_id, "model_id": config.model_id, "status": "success",
                "result": {"text": message.get("content", ""), "message": message},
                "error": None, "latency_ms": round((time.monotonic() - start) * 1000)}

    @app.post("/v1/stt/transcriptions")
    async def stt(request: Request):
        if speech.info["stt"]["status"] != "ready":
            return error("CAPABILITY_UNAVAILABLE", speech.info["stt"].get("error") or "STT not ready",
                         503, request=request, capability="stt")
        if slots.locked():
            return error("BACKEND_BUSY", "GPU stage is busy", 429, request=request, capability="stt", retryable=True)
        form = await request.form()
        upload = form.get("file") or form.get("audio")
        if not hasattr(upload, "read"):
            return error("INVALID_REQUEST", "Missing WAV file", 400, request=request, capability="stt")
        audio = await upload.read(2_000_001)
        await upload.close()
        if len(audio) > 2_000_000:
            return error("INVALID_REQUEST", "Audio exceeds 2 MB / 60 seconds", 413, request=request, capability="stt")
        import io
        import wave
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                if wav.getnframes() / wav.getframerate() > 60:
                    raise ValueError("Audio exceeds 60 seconds")
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
                    raise ValueError("Use mono PCM16 WAV at 16000 Hz")
        except (wave.Error, EOFError, ValueError) as exc:
            return error("INVALID_REQUEST", str(exc), 400, request=request, capability="stt")
        started = time.monotonic()
        async with slots:
            speech.info["stt"]["status"] = "busy"
            try:
                task = asyncio.create_task(asyncio.to_thread(speech.transcribe, audio))
                try:
                    text = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                if not text:
                    raise RuntimeError("Empty transcript")
            except Exception as exc:
                return error("MODEL_ERROR", str(exc), 502, request=request, capability="stt")
            finally:
                speech.info["stt"].update(status="ready", last_latency_ms=round((time.monotonic() - started) * 1000), first_chunk_ms=None)
        return {"contract_version": VERSION, "model_id": "whisper-base", "text": text,
                "latency_ms": speech.info["stt"]["last_latency_ms"]}

    @app.post("/v1/tts/synthesize")
    @app.post("/v1/tts/stream")
    async def tts(request: Request):
        if speech.info["tts"]["status"] != "ready":
            return error("CAPABILITY_UNAVAILABLE", speech.info["tts"].get("error") or "TTS not ready",
                         503, request=request, capability="tts")
        payload = await validate_json(request)
        if isinstance(payload, JSONResponse):
            return payload
        text = payload.get("input", payload).get("text") if isinstance(payload.get("input", payload), dict) else None
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            return error("INVALID_REQUEST", "text must contain 1..500 characters", 400, request=request, capability="tts")
        stream = payload.get("stream", request.url.path.endswith("/stream"))
        if type(stream) is not bool:
            return error("INVALID_REQUEST", "stream must be a boolean", 400, request=request, capability="tts")
        if stream:
            if payload.get("format", "pcm_s16le") != "pcm_s16le":
                return error("INVALID_REQUEST", "TTS stream format is pcm_s16le", 400, request=request, capability="tts")
            return await streams.tts(text.strip(), request)
        if slots.locked():
            return error("BACKEND_BUSY", "GPU stage is busy", 429, request=request, capability="tts", retryable=True)
        started = time.monotonic()
        async with slots:
            speech.info["tts"]["status"] = "busy"
            try:
                task = asyncio.create_task(asyncio.to_thread(speech.synthesize, text.strip()))
                try:
                    audio = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            except Exception as exc:
                return error("MODEL_ERROR", str(exc), 502, request=request, capability="tts")
            finally:
                speech.info["tts"].update(status="ready", last_latency_ms=round((time.monotonic() - started) * 1000), first_chunk_ms=None)
        return Response(audio, media_type="audio/wav", headers={
            "X-ViVi-Latency-Ms": str(speech.info["tts"]["last_latency_ms"]),
            "X-Audio-Sample-Rate": str(speech.info["tts"]["sample_rate"]),
            "X-Audio-Channels": "1", "X-Model-Id": "ZeroTTS", "X-Voice-Id": "VIVI",
        })

    return app


app = create_app(ServiceConfig.from_env()) if (os.environ.get("JETSON_AI_TOKEN") or os.environ.get("AI_SERVER_TOKEN") or not auth_required()) else None
