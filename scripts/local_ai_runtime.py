"""Run the real llama.cpp backend and Jetson gateway on a development machine."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class LocalAIServer:
    """Own only the processes created for this test and stop them on exit."""

    def __init__(self):
        self.processes: list[subprocess.Popen] = []
        self.logs = []
        self.url = ""
        self.token = secrets.token_hex(32)
        self.log_dir = ROOT / "data" / "local-ai-test"

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
            if not binary.is_file() or not model.is_file():
                print("Chuẩn bị Qwen3-1.7B Q4 và llama-server CPU (lần đầu cần mạng)...", flush=True)
                subprocess.run(["bash", str(ROOT / "scripts/setup_local_slm.sh"), "cpu"], cwd=ROOT, check=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            backend_port = free_port()
            env = os.environ.copy()
            env.update({"VIVI_SLM_PORT": str(backend_port), "VIVI_SLM_GPU_LAYERS": "0",
                        "VIVI_SLM_THREADS": "4", "VIVI_SLM_CONTEXT": "4096"})
            print("Đang nạp model Qwen3-1.7B thật trên CPU...", flush=True)
            started = time.monotonic()
            backend = self._start(["bash", str(ROOT / "scripts/run_local_slm.sh")], env, "llama")
            self._wait(backend, f"http://127.0.0.1:{backend_port}/health")
            load_ms = round((time.monotonic() - started) * 1000)
            gateway_port = free_port()
            self.url = f"http://127.0.0.1:{gateway_port}"
            env.update({"JETSON_AI_TOKEN": self.token,
                        "JETSON_AI_LLM_URL": f"http://127.0.0.1:{backend_port}",
                        "JETSON_AI_BACKEND_ID": "local-jetson-test",
                        "JETSON_AI_MODEL_ID": "vivi-qwen3-1.7b",
                        "JETSON_AI_LLM_MODEL_PATH": str(model.resolve()),
                        "JETSON_AI_LLM_LOG": str(self.log_dir / "llama.log"),
                        "JETSON_AI_LLM_PID": str(backend.pid),
                        "JETSON_AI_LLM_LOAD_MS": str(load_ms),
                        "JETSON_AI_ENABLE_SPEECH": "0"})
            gateway = self._start([
                sys.executable, "-m", "uvicorn", "jetson_ai.app:app", "--host", "127.0.0.1",
                "--port", str(gateway_port), "--workers", "1", "--no-access-log",
            ], env, "gateway")
            self._wait(gateway, self.url + "/v1/capabilities", authenticated=True)
            print(f"Jetson AI API chạy local: {self.url} | model=vivi-qwen3-1.7b | CPU", flush=True)
            print(f"Log: {self.log_dir}", flush=True)
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
