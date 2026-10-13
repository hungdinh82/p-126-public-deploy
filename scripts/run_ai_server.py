"""Serve resident AI models for the application's normal inference requests."""

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from local_ai_runtime import LocalAIServer

    from jetson_ai.config import auth_required

    parser = argparse.ArgumentParser(description="Run the ViVi AI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--gpu-layers", type=int, default=12)
    parser.add_argument("--tts-precision", choices=("auto", "fp32", "int8", "int8-gpu"), default="auto",
                        help="auto uses setup; int8 is CPU, int8-gpu requires TensorRT on CUDA")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.gpu_layers < 0:
        parser.error("Invalid port or GPU layer count")
    os.chdir(ROOT)
    if args.tts_precision != "auto":
        os.environ["JETSON_AI_TTS_PRECISION"] = args.tts_precision
    # Share per-machine auth overrides with the main app; existing env wins.
    for name, value in dotenv_values(ROOT / ".env").items():
        if value is not None and name in {"AI_SERVER_AUTH_REQUIRED", "JETSON_AI_AUTH_REQUIRED",
                                         "AI_SERVER_TOKEN", "JETSON_AI_TOKEN"}:
            os.environ.setdefault(name, value)
    log_dir = ROOT / "data" / "ai-server"
    saved = dotenv_values(log_dir / "service.env")
    token = os.environ.get("AI_SERVER_TOKEN") or os.environ.get("JETSON_AI_TOKEN") or saved.get("JETSON_AI_TOKEN")
    if auth_required() and token and len(token) < 32:
        parser.error("AI server token must contain at least 32 characters")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        with LocalAIServer(device=args.device, speech=True, port=args.port,
                           gpu_layers=args.gpu_layers, host=args.host,
                           log_dir=log_dir, backend_id="pc-ai-server", prepare=False,
                           token=token, require_auth=auth_required()) as server:
            print(f"App AI URL: http://{args.host}:{args.port}", flush=True)
            print("AI service đang chạy. Ctrl+C để dừng.", flush=True)
            while not stop.wait(1):
                if any(process.poll() is not None for process in server.processes):
                    raise RuntimeError(f"AI process stopped; logs: {log_dir}")
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
