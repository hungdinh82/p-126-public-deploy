"""Run the existing ViVi API and expose it through an ngrok HTTP tunnel."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env_file() -> dict[str, str]:
    """Read simple KEY=VALUE entries without sourcing executable shell content."""
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return values
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in {"NGROK_AUTHTOKEN", "NGROK_URL"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main() -> int:
    env_file = read_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("NGROK_URL", env_file.get("NGROK_URL")),
        help="static ngrok URL reserved in your ngrok account (or set NGROK_URL in .env)",
    )
    args = parser.parse_args()

    ngrok = shutil.which("ngrok")
    if ngrok is None:
        print("Không tìm thấy ngrok. Cài ngrok và cấu hình authtoken trước.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT))
    from src.vivi.config import settings

    backend = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=ROOT)
    tunnel: subprocess.Popen | None = None
    try:
        # Give Uvicorn a moment to bind before asking ngrok to forward the port.
        for _ in range(50):
            if backend.poll() is not None:
                return int(backend.returncode or 1)
            time.sleep(0.1)
        command = [ngrok, "http", str(settings.port)]
        if args.url:
            command.extend(["--url", args.url])
        tunnel_env = os.environ.copy()
        authtoken = os.environ.get("NGROK_AUTHTOKEN", env_file.get("NGROK_AUTHTOKEN"))
        if authtoken:
            tunnel_env["NGROK_AUTHTOKEN"] = authtoken
        tunnel = subprocess.Popen(command, cwd=ROOT, env=tunnel_env)
        print(
            f"ViVi local: http://127.0.0.1:{settings.port}\n"
            f"Ngrok đang chạy{f' tại {args.url}' if args.url else ''}. "
            "Xem output của ngrok để biết URL truy cập.",
            flush=True,
        )
        while backend.poll() is None and tunnel.poll() is None:
            time.sleep(0.25)
        return int((tunnel if tunnel.poll() is not None else backend).returncode or 0)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in (tunnel, backend):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in (tunnel, backend):
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
