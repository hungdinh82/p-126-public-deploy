"""Run the real llama.cpp backend and Jetson gateway on a development machine."""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class LocalAIServer:
    """Own model/gateway processes and stop them on exit."""

    def __init__(self, *, device="cpu", speech=False, port=None, gpu_layers=12,
                 log_dir=None, backend_id="local-jetson-test", prepare=True,
                 host="127.0.0.1", token=None, require_auth=True):
        self.device = device
        self.speech = speech
        self.port = port
        self.gpu_layers = gpu_layers
        self.processes: list[subprocess.Popen] = []
        self.logs = []
        self.url = ""
        self.token = (token or secrets.token_hex(32)) if require_auth else ""
        self.require_auth = require_auth
        self.log_dir = Path(log_dir) if log_dir else ROOT / "data" / ("local-ai-ui" if speech else "local-ai-test")
        self.backend_id = backend_id
        self.prepare = prepare
        self.host = host

    def _start(self, command: list[str], env: dict[str, str], name: str) -> subprocess.Popen:
        log = (self.log_dir / f"{name}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    def _wait(self, process: subprocess.Popen, url: str, *, authenticated: bool = False) -> None:
        deadline = time.monotonic() + 120
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
        with httpx.Client(timeout=2, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"AI process exited ({process.returncode}); logs: {self.log_dir}")
                try:
                    response = client.get(url, headers=headers)
                    if response.status_code == 200:
                        payload = response.json()
                        if authenticated:
                            if payload.get("capabilities", {}).get("llm", {}).get("status") == "ready":
                                return
                        elif payload.get("status") == "ok":
                            return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.25)
        raise RuntimeError(f"Model did not become ready within 120s; logs: {self.log_dir}")

    def __enter__(self) -> LocalAIServer:
        try:
            binary = Path(os.environ.get("LLAMA_SERVER_BIN", ROOT / "models/llama-server/llama-server"))
            model = Path(os.environ.get("VIVI_SLM_MODEL", ROOT / "models/vivi-slm/Qwen3-1.7B-Q4_K_M.gguf"))
            if (self.device == "cpu" and not binary.is_file()) or not model.is_file():
                if not self.prepare:
                    raise RuntimeError("Thiếu model hoặc llama-server. Chạy: bash scripts/ai_server.sh setup")
                print("Chuẩn bị Qwen3-1.7B Q4 và llama-server CPU (lần đầu cần mạng)...", flush=True)
                subprocess.run(["bash", str(ROOT / "scripts/setup_local_slm.sh"), "cpu"], cwd=ROOT, check=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if self.speech and self.require_auth:
                secret_file = self.log_dir / "service.env"
                fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(f"JETSON_AI_TOKEN={self.token}\n")
            backend_port = free_port()
            env = os.environ.copy()
            if not self.require_auth:
                env["AI_SERVER_AUTH_REQUIRED"] = "false"
            env.update({"VIVI_SLM_PORT": str(backend_port), "VIVI_SLM_GPU_LAYERS": str(self.gpu_layers if self.device == "cuda" else 0),
                        "VIVI_SLM_THREADS": "4", "VIVI_SLM_CONTEXT": "4096", "VIVI_SLM_MODEL": str(model)})
            if self.device == "cuda":
                import site
                dirs = [str(path) for base in site.getsitepackages()
                        for path in Path(base).glob("nvidia/*/lib")]
                env["LD_LIBRARY_PATH"] = ":".join(dirs + ["/usr/lib/wsl/lib", env.get("LD_LIBRARY_PATH", "")])
                command = [sys.executable, "-m", "uvicorn", "jetson_ai.python_llm:app",
                           "--host", "127.0.0.1", "--port", str(backend_port), "--no-access-log"]
            else:
                command = ["bash", str(ROOT / "scripts/run_local_slm.sh")]
            print(f"Đang nạp Qwen3-1.7B trên {self.device}...", flush=True)
            started = time.monotonic()
            backend = self._start(command, env, "llama")
            self._wait(backend, f"http://127.0.0.1:{backend_port}/health")
            load_ms = round((time.monotonic() - started) * 1000)
            if self.device == "cuda":
                import re
                log = (self.log_dir / "llama.log").read_text(errors="replace")
                if not re.search(r"offloaded [1-9]\d*/\d+ layers to GPU", log):
                    raise RuntimeError("SLM chưa xác nhận CUDA; xem llama.log")
            gateway_port = self.port or free_port()
            self.url = f"http://127.0.0.1:{gateway_port}"
            env.update({"JETSON_AI_TOKEN": self.token,
                        "JETSON_AI_LLM_URL": f"http://127.0.0.1:{backend_port}",
                        "JETSON_AI_BACKEND_ID": self.backend_id,
                        "JETSON_AI_MODEL_ID": "vivi-qwen3-1.7b",
                        "JETSON_AI_LLM_MODEL_PATH": str(model.resolve()),
                        "JETSON_AI_LLM_LOG": str(self.log_dir / "llama.log"),
                        "JETSON_AI_LLM_PID": str(backend.pid),
                        "JETSON_AI_LLM_LOAD_MS": str(load_ms),
                        "JETSON_AI_ENABLE_SPEECH": "1" if self.speech else "0",
                        "JETSON_AI_DEVICE": self.device,
                        "JETSON_AI_STT_ENGINE": "transformers",
                        "JETSON_AI_STT_MODEL": str(ROOT / "models/ai-speech/whisper"),
                        "JETSON_AI_TTS_MODEL": str(ROOT / "models/ai-speech/zerotts")})
            gateway = self._start([
                sys.executable, "-m", "uvicorn", "jetson_ai.app:app", "--host", self.host,
                "--port", str(gateway_port), "--workers", "1", "--no-access-log",
            ], env, "gateway")
            # UI stays available while speech weights load in the background.
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=2, trust_env=False) as client:
                while True:
                    if gateway.poll() is not None:
                        raise RuntimeError(f"Gateway exited; logs: {self.log_dir}")
                    try:
                        if client.get(self.url + "/health/live").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Gateway did not start within 30s")
                    time.sleep(0.25)
            print(f"AI server: http://{self.host}:{gateway_port} | model=vivi-qwen3-1.7b | {self.device}", flush=True)
            print(f"Log: {self.log_dir}", flush=True)
            if self.speech and self.require_auth:
                print(f"Nếu trình duyệt yêu cầu token: {self.log_dir / 'service.env'}", flush=True)
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in self.logs:
            log.close()

    def __exit__(self, *_exc) -> None:
        self.close()
