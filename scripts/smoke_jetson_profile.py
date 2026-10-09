"""Load all three Xavier GPU engines together and exercise real inference."""

import argparse
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import wave
from contextlib import ExitStack
from pathlib import Path

import httpx
import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jetson_ai.speech import SpeechModels  # noqa: E402


def unused_port(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def wait_ready(client, process, url, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Model process exited during smoke; inspect setup logs")
        try:
            if client.get(url, timeout=2).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"Model readiness timeout: {url}")


def smoke(args):
    unused_port(args.backend_port)
    unused_port(args.stt_port)
    if args.backend_port == args.stt_port:
        raise ValueError("SLM and STT ports must differ")
    logs = args.data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    llm_url = f"http://127.0.0.1:{args.backend_port}"
    stt_url = f"http://127.0.0.1:{args.stt_port}"
    with ExitStack() as stack:
        llm_log = stack.enter_context((logs / "setup-llama.log").open("w"))
        stt_log = stack.enter_context((logs / "setup-whisper.log").open("w"))
        llm = subprocess.Popen([
            str(args.data / "build" / "llama.cpp" / args.native_build / "bin" / "llama-server"),
            "--model", str(args.model), "--host", "127.0.0.1", "--port", str(args.backend_port),
            "--ctx-size", "4096", "--parallel", "1", "--n-gpu-layers", "99", "--reasoning", "off",
        ], stdout=llm_log, stderr=subprocess.STDOUT)
        stack.callback(stop, llm)
        client = stack.enter_context(httpx.Client(timeout=120, trust_env=False))
        wait_ready(client, llm, llm_url + "/health")
        stt = subprocess.Popen([
            str(args.data / "build" / "whisper.cpp" / args.native_build / "bin" / "whisper-server"),
            "--model", str(args.data / "models/speech/whisper/ggml-base.bin"),
            "--host", "127.0.0.1", "--port", str(args.stt_port), "--language", "vi",
        ], stdout=stt_log, stderr=subprocess.STDOUT)
        stack.callback(stop, stt)
        wait_ready(client, stt, stt_url + "/")
        text = (logs / "setup-llama.log").read_text(errors="replace")
        offloads = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", text)
        if not offloads or int(offloads[-1][0]) < 1:
            raise RuntimeError("SLM log did not confirm GPU offload")
        os.environ.update({
            "JETSON_AI_DEVICE": "cuda", "JETSON_AI_STT_ENGINE": "whisper_cpp",
            "JETSON_AI_STT_MODEL": str(args.data / "models/speech/whisper/ggml-base.bin"),
            "JETSON_AI_STT_URL": stt_url, "JETSON_AI_STT_LOG": str(logs / "setup-whisper.log"),
            "JETSON_AI_TTS_MODEL": str(args.data / "models/speech/zerotts"),
            "HF_HUB_OFFLINE": "1",
        })
        models = SpeechModels()
        models.load("stt")
        models.load("tts")
        for name in ("stt", "tts"):
            if models.info[name]["status"] != "ready" or not models.info[name]["using_gpu"]:
                raise RuntimeError(f"{name} CUDA load failed: {models.info[name]}")
        started = time.monotonic()
        result = client.post(llm_url + "/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "Trả lời ngắn: 2 cộng 3 bằng bao nhiêu?"}],
            "max_tokens": 48, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        result.raise_for_status()
        answer = result.json()["choices"][0]["message"]["content"]
        if not answer or not answer.strip():
            raise RuntimeError("SLM returned empty text")
        llm_ms = round((time.monotonic() - started) * 1000)
        started = time.monotonic()
        audio = models.synthesize("Xin chào bạn, đây là bài kiểm tra giọng nói trên máy Jetson.")
        with wave.open(io.BytesIO(audio), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getnframes() < 1:
                raise RuntimeError("TTS generated invalid/empty WAV")
            rate = wav.getframerate()
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
        tts_ms = round((time.monotonic() - started) * 1000)
        samples = resample_poly(samples, 16000, rate)
        converted = io.BytesIO()
        with wave.open(converted, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(np.clip(samples, -32768, 32767).astype("<i2").tobytes())
        started = time.monotonic()
        transcript = models.transcribe(converted.getvalue())
        if not transcript.strip():
            raise RuntimeError("STT returned empty text")
        report = {"llm_gpu_layers": offloads[-1], "llm_text": answer, "llm_ms": llm_ms,
                  "tts_ms": tts_ms, "tts_bytes": len(audio), "stt_text": transcript,
                  "stt_ms": round((time.monotonic() - started) * 1000), "speech": models.info}
        (logs / "setup-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--native-build", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--backend-port", type=int, default=1235)
    parser.add_argument("--stt-port", type=int, default=1236)
    smoke(parser.parse_args())
