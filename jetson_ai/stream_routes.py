"""Optional model streaming protocols; workflow remains on the Laptop."""

import asyncio
import hmac
import io
import json
import time
import wave

import anyio
import httpx
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from jetson_ai.scheduler import SchedulingError
from jetson_ai.streaming import close_iterator, next_or_none, sse, worker

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class BackendBusyError(RuntimeError):
    pass


class ModelStreams:
    def __init__(self, config, speech, scheduler, backend_status, error, record_llm, transport=None):
        self.config, self.speech, self.scheduler = config, speech, scheduler
        self.backend_status, self.error, self.record_llm = backend_status, error, record_llm
        self.transport = transport

    async def chat(self, payload, request, timeout_ms=None, envelope=None):
        state = await self.backend_status()
        if state != "ready":
            return self.error(
                "MODEL_LOADING" if state == "loading" else "CAPABILITY_UNAVAILABLE",
                "LLM backend is not ready",
                503,
                request=request,
                retryable=True,
            )
        deadline = time.monotonic() + timeout_ms / 1000 if timeout_ms else None
        lease = await self.scheduler.acquire(
            "llm", timeout=timeout_ms / 1000 if timeout_ms else None, call_id=request.headers.get("x-vivi-call-id", "")
        )
        started = time.monotonic()
        timeout = (
            min(self.config.timeout_seconds, max(0.001, deadline - time.monotonic()))
            if deadline
            else self.config.timeout_seconds
        )
        client = httpx.AsyncClient(transport=self.transport, timeout=timeout)
        upstream = None
        closed = False
        first_ms = None

        async def cleanup():
            nonlocal closed
            if closed:
                return
            closed = True
            with anyio.CancelScope(shield=True):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                    await client.aclose()
                finally:
                    self.record_llm(round((time.monotonic() - started) * 1000), first_ms)
                    lease.release()

        try:
            outgoing = client.build_request(
                "POST", self.config.backend_url.rstrip("/") + "/v1/chat/completions", json={**payload, "stream": True}
            )
            async with asyncio.timeout(timeout):
                upstream = await client.send(outgoing, stream=True)
            if upstream.status_code >= 400:
                status = 429 if upstream.status_code == 429 else 502
                await cleanup()
                return self.error(
                    "BACKEND_BUSY" if status == 429 else "MODEL_ERROR",
                    "LLM backend rejected streaming request",
                    status,
                    request=request,
                    retryable=upstream.status_code in {429, 503},
                )
            if "text/event-stream" not in upstream.headers.get("content-type", ""):
                await cleanup()
                return self.error("MODEL_ERROR", "LLM backend did not return SSE", 502, request=request)
        except (httpx.TimeoutException, TimeoutError):
            await cleanup()
            return self.error("SERVICE_TIMEOUT", "LLM backend timed out", 504, request=request, retryable=True)
        except httpx.HTTPError:
            await cleanup()
            return self.error("MODEL_ERROR", "LLM backend connection failed", 502, request=request)
        except BaseException:
            await cleanup()
            raise

        base = {
            "contract_version": "1.0",
            "backend_id": self.config.backend_id,
            "model_id": self.config.model_id,
            "queue_ms": lease.queue_ms,
            **(envelope or {}),
        }

        async def events():
            nonlocal first_ms
            text, tools = [], {}
            finished = False
            try:
                # Total deadline includes the first token and slow streams.
                remaining = max(0.001, timeout - (time.monotonic() - started))
                async with asyncio.timeout(remaining):
                    async for line in upstream.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            finished = True
                            break
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise ValueError("Backend stream error")
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            content = delta.get("content") or ""
                            if content or delta.get("tool_calls"):
                                if first_ms is None:
                                    first_ms = round((time.monotonic() - started) * 1000)
                                text.append(content)
                            for tool in delta.get("tool_calls", []):
                                index = tool["index"]
                                item = tools.setdefault(
                                    index, {"type": "function", "id": "", "function": {"name": "", "arguments": ""}}
                                )
                                item["id"] += tool.get("id", "")
                                for key in ("name", "arguments"):
                                    item["function"][key] += tool.get("function", {}).get(key, "")
                            if envelope is not None:
                                yield sse(
                                    {**base, "delta": delta, "text": content, "first_chunk_ms": first_ms}, "delta"
                                )
                        if envelope is None:
                            chunk["model"] = self.config.model_id
                            yield sse(chunk)
                if not finished:
                    raise ValueError("Backend stream ended before [DONE]")
                if envelope is not None:
                    message = {"role": "assistant", "content": "".join(text)}
                    if tools:
                        message["tool_calls"] = [tools[index] for index in sorted(tools)]
                    yield sse(
                        {
                            **base,
                            "status": "success",
                            "result": {"text": message["content"], "message": message},
                            "error": None,
                            "first_chunk_ms": first_ms,
                            "latency_ms": round((time.monotonic() - started) * 1000),
                        },
                        "done",
                    )
                else:
                    yield sse("[DONE]")
            except Exception as exc:
                code = "SERVICE_TIMEOUT" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "MODEL_ERROR"
                event = {
                    **base,
                    "status": "error",
                    "error": {"code": code, "message": "LLM stream failed", "retryable": False},
                }
                yield sse(event, "error" if envelope is not None else None)
                if envelope is None:
                    yield sse("[DONE]")
            finally:
                await cleanup()

        return StreamingResponse(
            events(), media_type="text/event-stream", headers=STREAM_HEADERS, background=BackgroundTask(cleanup)
        )

    async def tts(self, text, request):
        lease = await self.scheduler.acquire("tts", call_id=request.headers.get("x-vivi-call-id", ""))
        started = time.monotonic()
        state = self.speech.info["tts"]
        state["status"] = "busy"
        chunks = iter(self.speech.synthesize_stream(text))
        closed = False
        first_ms = None

        async def cleanup():
            nonlocal closed
            if not closed:
                closed = True
                try:
                    await close_iterator(chunks)
                finally:
                    state.update(
                        status="ready",
                        last_latency_ms=round((time.monotonic() - started) * 1000),
                        first_chunk_ms=first_ms,
                    )
                    lease.release()

        # Detect first-step errors before committing an HTTP 200 audio stream.
        try:
            first = await asyncio.wait_for(worker(next_or_none, chunks), self.config.timeout_seconds)
            if not first:
                raise ValueError("No audio generated")
            first_ms = round((time.monotonic() - started) * 1000)
            lease.pause()
        except BaseException as exc:
            await cleanup()
            if isinstance(exc, Exception):
                timed_out = isinstance(exc, TimeoutError)
                return self.error(
                    "SERVICE_TIMEOUT" if timed_out else "MODEL_ERROR",
                    "TTS stream failed",
                    504 if timed_out else 502,
                    request=request,
                    capability="tts",
                )
            raise

        async def audio():
            try:
                yield first
                while True:
                    remaining = self.config.timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("TTS stream deadline exceeded")
                    async with lease.step(timeout=remaining):
                        chunk = await asyncio.wait_for(worker(next_or_none, chunks), remaining)
                    if chunk is None:
                        break
                    yield chunk
            finally:
                await cleanup()

        return StreamingResponse(
            audio(),
            media_type="application/octet-stream",
            background=BackgroundTask(cleanup),
            headers={
                **STREAM_HEADERS,
                "X-Audio-Sample-Rate": str(state["sample_rate"]),
                "X-Audio-Channels": "1",
                "X-Audio-Format": "pcm_s16le",
                "X-ViVi-First-Chunk-Ms": str(first_ms),
                "X-Model-Id": "ZeroTTS",
            },
        )

    def install(self, app):
        @app.websocket("/v1/stt/stream")
        async def stt(socket: WebSocket):
            await socket.accept()
            pcm = bytearray()
            state = self.speech.info["stt"]
            started = time.monotonic()
            updates = 0
            first_ms = None

            async def fail(code, message):
                await socket.send_json({"type": "error", "error": {"code": code, "message": message}})
                await socket.close(code=1008)

            async def decode():
                async with await self.scheduler.acquire("stt"):
                    state["status"] = "busy"
                    output = io.BytesIO()
                    with wave.open(output, "wb") as wav:
                        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        wav.writeframes(pcm)
                    try:
                        return await worker(self.speech.transcribe, output.getvalue())
                    finally:
                        state["status"] = "ready"

            try:
                async with asyncio.timeout(90):
                    setup = await socket.receive_json()
                    supplied = socket.headers.get("authorization", "")
                    token = setup.get("token", "") if isinstance(setup, dict) else ""
                    if self.config.auth_required and not (
                        hmac.compare_digest(supplied, f"Bearer {self.config.token}")
                        or isinstance(token, str)
                        and hmac.compare_digest(token.encode(), self.config.token.encode())
                    ):
                        await fail("UNAUTHORIZED", "Invalid service token")
                        return
                    if state["status"] not in {"ready", "busy"}:
                        await fail(
                            "BACKEND_BUSY" if state["status"] == "busy" else "CAPABILITY_UNAVAILABLE", "STT not ready"
                        )
                        return
                    if (
                        not isinstance(setup, dict)
                        or setup.get("type") != "start"
                        or setup.get("sample_rate") != 16000
                        or setup.get("format") != "pcm_s16le"
                        or setup.get("channels", 1) != 1
                    ):
                        await fail("INVALID_REQUEST", "Send start with mono PCM16 at 16000 Hz")
                        return
                    await socket.send_json({"type": "ready", "max_seconds": 60, "mode": "rolling_utterance"})
                    last_size = 0
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        data = message.get("bytes")
                        if data is not None:
                            if len(data) > 65536 or len(data) % 2 or len(pcm) + len(data) > 16000 * 2 * 60:
                                await fail("INVALID_REQUEST", "PCM chunk must be even, <=64 KB; utterance <=60 seconds")
                                return
                            pcm.extend(data)
                            if len(pcm) - last_size >= 32000:
                                last_size = len(pcm)
                                text = await decode()
                                updates += 1
                                first_ms = (
                                    first_ms if first_ms is not None else round((time.monotonic() - started) * 1000)
                                )
                                await socket.send_json(
                                    {"type": "partial", "text": text, "revision": updates, "first_chunk_ms": first_ms}
                                )
                            await socket.send_json({"type": "ack", "received_bytes": len(pcm)})
                        elif message.get("text") == "stop":
                            if not pcm:
                                await fail("INVALID_REQUEST", "No audio received")
                                return
                            text = await decode()
                            if not text:
                                await fail("MODEL_ERROR", "Empty transcript")
                                return
                            latency = round((time.monotonic() - started) * 1000)
                            if first_ms is None:
                                first_ms = latency
                            state.update(last_latency_ms=latency, first_chunk_ms=first_ms)
                            await socket.send_json(
                                {"type": "done", "text": text, "latency_ms": latency, "first_chunk_ms": first_ms}
                            )
                            await socket.close()
                            return
                        else:
                            await fail("INVALID_REQUEST", "Expected PCM binary or stop")
                            return
            except WebSocketDisconnect:
                pass
            except Exception as exc:
                try:
                    code = (
                        exc.code
                        if isinstance(exc, SchedulingError)
                        else "SERVICE_TIMEOUT"
                        if isinstance(exc, TimeoutError)
                        else "MODEL_ERROR"
                    )
                    await fail(code, "STT stream failed")
                except (RuntimeError, WebSocketDisconnect):
                    pass
