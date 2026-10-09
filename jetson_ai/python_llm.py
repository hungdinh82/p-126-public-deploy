"""Local CUDA bridge; Xavier uses its native llama-server instead."""
import os
import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

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
    if not lock.acquire(blocking=False):
        raise HTTPException(429, "Model is busy")
    try:
        allowed = {key: payload[key] for key in
                   ("messages", "max_tokens", "temperature", "top_p", "tools", "tool_choice", "response_format")
                   if key in payload}
        result = model.create_chat_completion(**allowed, stream=False)
        for choice in result["choices"]:
            content = choice["message"].get("content")
            if isinstance(content, str):
                choice["message"]["content"] = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S).strip()
        result["model"] = "vivi-qwen3-1.7b"
        return result
    finally:
        lock.release()
