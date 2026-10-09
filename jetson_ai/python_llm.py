"""Local CUDA bridge; Xavier uses its native llama-server instead."""
import os
import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from jetson_ai.streaming import close_iterator, next_or_none, sse, worker

model = None
lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app):
    global model
    from llama_cpp import Llama
    model = Llama(model_path=os.environ["VIVI_SLM_MODEL"],
                  n_gpu_layers=int(os.environ.get("VIVI_SLM_GPU_LAYERS", "12")),
                  n_ctx=2048, n_batch=128, n_threads=4, verbose=True)
    from llama_cpp.llama_chat_format import Jinja2ChatFormatter
    template = model.metadata.get("tokenizer.chat_template")
    if template:
        model.chat_handler = Jinja2ChatFormatter(
            template="{% set enable_thinking = false %}" + template,
            eos_token=model.detokenize([model.token_eos()], special=True).decode("utf-8"),
            bos_token=model.detokenize([model.token_bos()], special=True).decode("utf-8"),
        ).to_chat_handler()
    yield
    model.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok" if model is not None else "loading"}


@app.post("/v1/chat/completions")
def chat(payload: dict):
    if type(payload.get("stream", False)) is not bool:
        raise HTTPException(400, "stream must be a boolean")
    if not lock.acquire(blocking=False):
        raise HTTPException(429, "Model is busy")
    try:
        allowed = {key: payload[key] for key in
                   ("messages", "max_tokens", "temperature", "top_p", "tools", "tool_choice", "response_format")
                   if key in payload}
        if payload.get("stream"):
            chunks = model.create_chat_completion(**allowed, stream=True)
            released = False

            async def cleanup():
                nonlocal released
                if not released:
                    released = True
                    try:
                        await close_iterator(chunks)
                    finally:
                        lock.release()

            async def events():
                try:
                    while True:
                        chunk = await worker(next_or_none, chunks)
                        if chunk is None:
                            break
                        chunk["model"] = "vivi-qwen3-1.7b"
                        yield sse(chunk)
                    yield sse("[DONE]")
                except Exception:
                    yield sse({"error": {"code": "MODEL_ERROR", "message": "Generation failed"}})
                    yield sse("[DONE]")
                finally:
                    await cleanup()

            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                                     background=BackgroundTask(cleanup))
        result = model.create_chat_completion(**allowed, stream=False)
        for choice in result["choices"]:
            content = choice["message"].get("content")
            if isinstance(content, str):
                choice["message"]["content"] = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S).strip()
        result["model"] = "vivi-qwen3-1.7b"
        return result
    finally:
        if not payload.get("stream") or "chunks" not in locals():
            lock.release()
