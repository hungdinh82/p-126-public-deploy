#!/usr/bin/env python3
"""Nhập text để thử public API ViVi, RAG và xe memory simulator trên local."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
HELP = """Nhập câu hỏi/lệnh xe; Enter để gửi.
  /approve       xác nhận thao tác đang chờ
  /deny          từ chối thao tác đang chờ
  /state         xem trạng thái xe mô phỏng
  /driving on    bật trạng thái đang lái (/driving off để tắt)
  /new           bắt đầu session mới
  /help          xem hướng dẫn
  /quit          thoát
"""


def configure(args: argparse.Namespace, directory: Path) -> None:
    """Override only this process; use isolated history and a simulated vehicle."""
    os.environ.update({
        "APP_ENV": "test", "VIVI_RUNTIME_PROFILE": "test",
        "VIVI_VEHICLE_PROVIDER": "memory", "VIVI_MEMORY_ENABLED": "false",
        "VIVI_DATA_DIR": str(directory), "RAG_HISTORY_DB": str(directory / "history.sqlite3"),
        "VIVI_STORE_AUDIO": "false", "VIVI_STORE_TRANSCRIPTS": "false",
        "STT_PROVIDER": "off", "STT_FALLBACK_PROVIDER": "off", "TTS_PROVIDER": "off",
        "STT_FALLBACK_PRELOAD": "false", "PHOWHISPER_PRELOAD": "false",
        "ZEROTTS_PRELOAD": "false", "RAG_RETRIEVAL_MODE": args.retrieval,
        "LLM_TIMEOUT_SECONDS": str(args.timeout),
    })
    if args.mode == "rules":
        os.environ.update({
            "LLM_PROVIDER": "rules", "JETSON_AI_POLICY": "disabled",
            "VIVI_RAG_LOCAL_ONLY": "true", "LOCAL_LLM_MODEL": "",
            "OPENAI_API_KEY": "", "GOOGLE_API_KEY": "", "GEMINI_API_KEY": "",
            "OPENROUTER_API_KEY": "",
        })
    else:
        url = args.jetson_url or os.environ.get("JETSON_AI_URL", "")
        token = os.environ.get("JETSON_AI_TOKEN", "")
        if not url or not token:
            raise ValueError("Chế độ jetson cần --jetson-url (hoặc JETSON_AI_URL) và JETSON_AI_TOKEN.")
        os.environ.update({
            "LLM_PROVIDER": "local", "JETSON_AI_URL": url.rstrip("/"),
            "JETSON_AI_POLICY": args.policy,
            "LOCAL_LLM_BASE_URL": url.rstrip("/") + "/v1",
            "LOCAL_LLM_API_KEY": token,
            "LOCAL_LLM_MODEL": os.environ.get("LOCAL_LLM_MODEL") or "vivi-qwen3-1.7b",
            "VIVI_RAG_LOCAL_ONLY": "true" if args.policy == "offline_only" else "false",
        })


class Playground:
    def __init__(self, client: httpx.AsyncClient, args: argparse.Namespace):
        self.client = client
        self.args = args
        self.session_id = f"test-{uuid4().hex[:12]}"
        self.pending: str | None = None
        self.state: dict = {}

    async def request(self, method: str, path: str, body: dict | None = None) -> dict:
        response = await asyncio.wait_for(
            self.client.request(method, path, json=body), timeout=self.args.timeout,
        )
        response.raise_for_status()
        return response.json()

    def show(self, result: dict) -> None:
        self.state = result.get("vehicle_state", self.state)
        confirmation = result.get("confirmation")
        self.pending = confirmation.get("confirmation_id") if confirmation else None
        if self.args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"\nViVi: {result.get('message', '')}")
        print(f"  backend={result.get('provider')} | route={result.get('route')} | status={result.get('status')}")
        print(f"  session={result.get('session_id')} | turn={result.get('turn_id')}")
        print(f"  latency={result.get('latency_ms', {}).get('total', 0):g} ms")
        if result.get("action"):
            action = result["action"]
            print(f"  action={action['intent']} {json.dumps(action['arguments'], ensure_ascii=False)}")
        for source in result.get("evidence", []):
            print(f"  nguồn={source['source_id']} | {source.get('section', '')}")
        if self.pending:
            print("  Đang chờ xác nhận: nhập /approve hoặc /deny.")
        if result.get("error"):
            print(f"  lỗi={result['error']}")
        print()

    async def submit(self, text: str) -> bool:
        if not text.strip():
            return True
        command = text.strip().lower()
        if command == "/help":
            print(HELP)
            return True
        if command == "/new":
            self.session_id = f"test-{uuid4().hex[:12]}"
            self.pending = None
            self.state = {}
            print(f"Session mới: {self.session_id}")
            return True
        if command == "/state":
            self.state = await self.request("GET", f"/api/v1/vehicle/state?session_id={self.session_id}")
            print(json.dumps(self.state, ensure_ascii=False, indent=2))
            return True
        if command.startswith("/driving"):
            if command not in {"/driving on", "/driving off"}:
                print("Dùng /driving on hoặc /driving off.")
                return False
            result = await self.request("PUT", "/api/v1/demo/vehicle/driving", {
                "session_id": self.session_id, "driving": command.endswith(" on"),
            })
            print(json.dumps(result, ensure_ascii=False, indent=2))
            self.state = result
            return True
        if command in {"/approve", "/deny"}:
            if not self.pending:
                print("Không có thao tác đang chờ xác nhận.")
                return False
            result = await self.request("POST", f"/api/v1/confirmations/{self.pending}", {
                "session_id": self.session_id, "turn_id": uuid4().hex,
                "decision": "approve" if command == "/approve" else "deny",
            })
        elif command.startswith("/"):
            print("Lệnh chưa biết; nhập /help.")
            return False
        else:
            result = await self.request("POST", "/api/v1/turn", {
                "transcript": text.strip(), "session_id": self.session_id, "turn_id": uuid4().hex,
            })
        self.show(result)
        return result.get("status") != "error"


async def run(args: argparse.Namespace) -> int:
    # Import after env overrides: exercise the existing /api/v1 composition root.
    from src.vivi.api.app import app
    from src.vivi.api.runtime import runtime

    failures = 0
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-test") as client:
            playground = Playground(client, args)
            health = await playground.request("GET", "/api/v1/health")
            print(f"ViVi local test | mode={args.mode} | retrieval={args.retrieval} | xe=memory simulator")
            print(f"Handbook: {health['orchestration']['handbook_path']}")
            if not health["orchestration"]["handbook_available"]:
                print("Lỗi: thiếu handbook SQLite trên Laptop.", file=sys.stderr)
                return 1
            print("History test dùng thư mục tạm, được xóa khi thoát.")
            if not args.text:
                print(HELP)
            inputs = iter(args.text) if args.text else None
            while True:
                try:
                    text = next(inputs) if inputs is not None else input("Bạn> ")
                except (StopIteration, EOFError):
                    break
                if text.strip().lower() in {"/quit", "/exit"}:
                    break
                if inputs is not None:
                    print(f"Bạn> {text}")
                try:
                    if not await playground.submit(text):
                        failures += 1
                except httpx.HTTPStatusError as exc:
                    print(f"Lỗi HTTP {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
                    failures += 1
                except TimeoutError:
                    print(f"Lượt vượt quá timeout {args.timeout:g}s; kết quả chưa được xác nhận.", file=sys.stderr)
                    failures += 1
                    break
    finally:
        runtime.close()
    return 1 if failures else 0


async def run_llm(args: argparse.Namespace) -> int:
    """Call the actual Jetson model API directly, without the workflow classifier."""
    url = os.environ["JETSON_AI_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["JETSON_AI_TOKEN"]}
    messages = [{"role": "system", "content": "Bạn là trợ lý hữu ích. Trả lời bằng tiếng Việt, ngắn gọn."}]
    failures = 0
    async with httpx.AsyncClient(timeout=args.timeout, trust_env=False, headers=headers) as client:
        capability = await client.get(url + "/v1/capabilities")
        capability.raise_for_status()
        llm = capability.json()["capabilities"]["llm"]
        print(f"Test model API | backend=jetson_ai | model={llm['model_id']} | status={llm['status']}")
        if llm["status"] != "ready":
            print("LLM chưa ready. Kiểm tra model server trước khi gửi input.", file=sys.stderr)
            return 1
        if not args.text:
            print("Nhập câu hỏi rồi Enter. /new xóa hội thoại; /quit hoặc Ctrl+C để thoát.")
        inputs = iter(args.text) if args.text else None
        while True:
            try:
                text = next(inputs) if inputs is not None else input("Bạn> ")
            except (StopIteration, EOFError):
                break
            command = text.strip().lower()
            if command in {"/quit", "/exit"}:
                break
            if command == "/new":
                messages = messages[:1]
                print("Đã xóa hội thoại test.")
                continue
            if not text.strip():
                continue
            if inputs is not None:
                print(f"Bạn> {text}")
            request_messages = [*messages, {"role": "user", "content": text.strip()}]
            try:
                response = await client.post(url + "/v1/llm/generate", json={
                    "contract_version": "1.0", "capability": "llm", "request_id": uuid4().hex,
                    "call_id": uuid4().hex, "attempt": 1, "timeout_ms": int(args.timeout * 1000),
                    "input": {"messages": request_messages, "max_tokens": 256, "temperature": 0.2,
                              "chat_template_kwargs": {"enable_thinking": False}},
                })
                response.raise_for_status()
                result = response.json()
                answer = result["result"]["text"]
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(f"\nAI: {answer}")
                    print(f"  backend={result['backend_id']} | model={result['model_id']} | {result['latency_ms']} ms\n")
                messages = [messages[0], *request_messages[1:], {"role": "assistant", "content": answer}]
                messages = [messages[0], *messages[1:][-8:]]
            except httpx.HTTPStatusError as exc:
                print(f"Lỗi HTTP {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
                failures += 1
            except httpx.TimeoutException:
                print("Model API quá thời gian chờ.", file=sys.stderr)
                failures += 1
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("rules", "local", "jetson"), default="rules")
    parser.add_argument("--target", choices=("pipeline", "llm"), default="pipeline",
                        help="pipeline: RAG/tool; llm: hỏi trực tiếp model API")
    parser.add_argument("--jetson-url", help="Ví dụ http://100.x.x.x:8789; token lấy từ JETSON_AI_TOKEN")
    parser.add_argument("--policy", choices=("offline_only", "prefer_local"), default="offline_only")
    parser.add_argument("--retrieval", choices=("sqlite", "sqlite_local"), default="sqlite")
    parser.add_argument("--text", action="append", help="Gửi một input; lặp --text để thử nhiều lượt, hỗ trợ /approve")
    parser.add_argument("--json", action="store_true", help="In đầy đủ response JSON")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    if args.target == "llm" and args.mode == "rules":
        parser.error("--target llm cần --mode local hoặc --mode jetson")
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    try:
        from local_ai_runtime import LocalAIServer
        with LocalAIServer() if args.mode == "local" else nullcontext() as local_server:
            if local_server is not None:
                args.jetson_url = local_server.url
                os.environ["JETSON_AI_TOKEN"] = local_server.token
                os.environ["LOCAL_LLM_MODEL"] = "vivi-qwen3-1.7b"
            with TemporaryDirectory(prefix="vivi-pipeline-test-") as directory:
                configure(args, Path(directory))
                return asyncio.run(run_llm(args) if args.target == "llm" else run(args))
    except (ValueError, RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as exc:
        print(f"Không khởi động được test: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nĐã dừng test.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
