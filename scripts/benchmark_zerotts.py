"""Measure one ZeroTTS profile and save audio for a listening comparison."""

import argparse
import json
import os
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jetson_ai.speech import SpeechModels  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("models/ai-speech"))
    parser.add_argument("--precision", choices=("fp32", "int8", "int8-gpu"), required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--text", default="Xin chào bạn, tôi là trợ lý ViVi.")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or not args.text.strip():
        parser.error("runs must be positive and text cannot be empty")
    os.environ.update(JETSON_AI_TTS_MODEL=str(args.root / "zerotts"),
                      JETSON_AI_TTS_PRECISION=args.precision, JETSON_AI_DEVICE=args.device)
    models = SpeechModels()
    models.load("tts")
    if models.info["tts"]["status"] != "ready":
        raise RuntimeError(models.info["tts"]["error"])
    args.output.mkdir(parents=True, exist_ok=True)
    model, voice = models.models["tts"]
    results = []
    # One warmup; use the same random draws for both profiles and each repeat.
    for index in range(args.runs + 1):
        np.random.seed(args.seed)
        started = time.perf_counter()
        first = None
        chunks = []
        stream = model.synthesize_stream(args.text, voice=voice, max_frames=300)
        try:
            for chunk in stream:
                chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
                if not np.isfinite(chunk).all():
                    raise RuntimeError("TTS produced non-finite samples")
                if chunk.size:
                    if first is None:
                        first = time.perf_counter() - started
                    chunks.append(chunk)
        finally:
            stream.close()
        elapsed = time.perf_counter() - started
        if not chunks:
            raise RuntimeError("TTS produced empty audio")
        audio = np.concatenate(chunks)
        if not np.any(audio):
            raise RuntimeError("TTS produced silent audio")
        duration = audio.size / model.sample_rate
        stats = {"synthesis_s": elapsed, "audio_s": duration, "rtf": elapsed / duration,
                 "first_audio_ms": first * 1000, "peak": float(np.abs(audio).max())}
        print(json.dumps({"warmup": index == 0, **stats}), flush=True)
        if index:
            results.append(stats)
            with wave.open(str(args.output / f"sample-{index}.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(model.sample_rate)
                wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    report = {"model": models.info["tts"], "text": args.text, "seed": args.seed,
              "max_frames": 300, "runs": results,
              "mean_rtf": statistics.mean(row["rtf"] for row in results),
              "mean_first_audio_ms": statistics.mean(row["first_audio_ms"] for row in results)}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Saved audio and timings: {args.output}", flush=True)


if __name__ == "__main__":
    main()
