#!/usr/bin/env python3
"""Measure Jetson model stages with ViVi's production planner prompt."""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import time
import uuid
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def planner_payload():
    from src.vivi.agents.prompts import VOICE_PERSONA
    from src.vivi.capabilities.registry import catalog
    from src.vivi.conversation.context import conversation_view
    from src.vivi.conversation.guardrails import TOPIC_INSTRUCTION
    from src.vivi.dialogue.contracts import SessionState, semantic_schema
    from src.vivi.dialogue.planner import SEMANTIC_INSTRUCTION, SemanticResolver
    from src.vivi.structured_llm import strict_schema

    session = SessionState()
    instruction = VOICE_PERSONA + SEMANTIC_INSTRUCTION + TOPIC_INSTRUCTION + (
        "\nFor a disallowed intent return unsupported with a brief safe alternative, "
        "no drafts. For emergencies return reply with immediate safe support, no drafts. "
        "Return compact JSON: omit null argument slots and unused optional fields; "
        "include only the operations and slots needed for this turn."
    )
    prompt = json.dumps({
        "utterance": "Mình đang đi, tìm trạm sạc gần nhất rồi chỉ đường đến đó giúp mình.",
        "tasks": SemanticResolver.context(session),
        "conversation_context": conversation_view(session),
        "recent_turns": [
            {"query": "Chỉ mình cách bật điều hòa.", "answer": "Bạn có thể dùng nút Climate trên màn hình xe."},
            {"query": "Pin còn bao nhiêu phần trăm?", "answer": "Mình chưa đọc được trạng thái pin lúc này."},
            {"query": "Tìm quán cà phê gần đây.", "answer": "Bạn muốn tìm quanh vị trí hiện tại phải không?"},
            {"query": "Ừ, gần đây thôi.", "answer": "Mình sẽ tìm quanh vị trí hiện tại."},
            {"query": "Thôi, hỏi đường đến sân bay Nội Bài.", "answer": "Bạn muốn bắt đầu hành trình sau khi xem trước lộ trình không?"},
            {"query": "Chưa, tìm trạm sạc trước.", "answer": "Mình sẽ tìm trạm sạc gần nhất."},
        ],
        "explicit_user_memory": [],
        "capabilities": catalog(True, conversation=True, information=True),
    }, ensure_ascii=False)
    schema = strict_schema(semantic_schema())
    tools = [{"type": "function", "function": {
        "name": "interpret_turn", "description": "Interpret the complete turn into semantic events.",
        "parameters": schema,
    }}]
    return instruction, prompt, tools


def wav_silence():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8795")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--stages", nargs="+", choices=("llm", "stt", "tts"), default=("llm", "stt", "tts"))
    args = parser.parse_args()
    token = os.environ.get("JETSON_AI_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    results = {"host": os.uname().nodename, "base_url": args.base_url, "rounds": args.rounds}
    with httpx.Client(timeout=240, trust_env=False) as client:
        if "llm" in args.stages:
            system, user, tools = planner_payload()
            payload = {
                "model": "vivi-qwen3-1.7b",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "tools": tools, "tool_choice": "required", "parallel_tool_calls": False,
                "temperature": 0, "max_tokens": 2048,
            }
            samples = []
            token_counts = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                response = client.post(args.base_url + "/v1/chat/completions", json=payload,
                                       headers={**headers, "X-ViVi-Timeout-Ms": "220000",
                                                "X-ViVi-Call-Id": uuid.uuid4().hex})
                response.raise_for_status()
                body = response.json()
                samples.append(round((time.perf_counter() - start) * 1000))
                usage = body.get("usage", {})
                token_counts.append({k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")})
            results["llm"] = {
                "system_chars": len(system), "user_context_chars": len(user),
                "schema_chars": len(json.dumps(tools, ensure_ascii=False)),
                "max_tokens": 2048, "wall_ms": samples,
                "median_ms": round(statistics.median(samples)), "usage": token_counts,
            }
        if "stt" in args.stages:
            samples = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                response = client.post(args.base_url + "/v1/stt/transcriptions",
                                       files={"file": ("benchmark.wav", wav_silence(), "audio/wav")},
                                       headers=headers)
                response.raise_for_status()
                samples.append(round((time.perf_counter() - start) * 1000))
            results["stt"] = {"audio_seconds": 1, "wall_ms": samples,
                              "median_ms": round(statistics.median(samples))}
        if "tts" in args.stages:
            samples = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                with client.stream("POST", args.base_url + "/v1/tts/stream",
                                   json={"text": "Mình đã nhận yêu cầu. Mình sẽ tìm trạm sạc gần nhất và chuẩn bị đường đi cho bạn.",
                                         "stream": True}, headers=headers) as response:
                    response.raise_for_status()
                    first_chunk_ms = None
                    chunks = iter(response.iter_bytes())
                    for chunk in chunks:
                        if chunk:
                            first_chunk_ms = round((time.perf_counter() - start) * 1000)
                            for _ in chunks:
                                pass
                            break
                    total_ms = round((time.perf_counter() - start) * 1000)
                    samples.append({"first_chunk_ms": first_chunk_ms, "total_ms": total_ms,
                                    "server_first_chunk_ms": response.headers.get("X-ViVi-First-Chunk-Ms")})
            results["tts"] = samples
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
