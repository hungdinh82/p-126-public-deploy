"""Record real "ViVi ơi" / "Hey ViVi" calls, scoring each one as it is recorded.

    .venv-wake/bin/python scripts/record_wake.py --speaker phong
    .venv-wake/bin/python scripts/record_wake.py --speaker lan --count 30

Each take: a prompt, a short countdown, then 3 s of recording. The current model
scores the take on the spot, so a session doubles as a real-voice recall test;
the summary shows how many calls it would have caught. Takes are saved to
data/wake/real/positive/<speaker>-<phrase>-NNN.wav and train_wake.py --real uses
them. Vary distance, speed, loudness and where you sit; mumbled calls count.
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RATE = 16_000
CHUNK = 1280
PHRASES = {"oi": "Vi Vi ơi", "hey": "Hey Vi Vi"}


def peak_score(detector, audio: np.ndarray) -> float:
    detector.reset()
    padded = np.concatenate([np.zeros(RATE, np.int16), audio, np.zeros(RATE // 2, np.int16)])
    return max(float(next(iter(detector.predict(padded[i:i + CHUNK]).values())))
               for i in range(0, len(padded) - CHUNK + 1, CHUNK))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--speaker", required=True, help="a short name, so clips of one person stay together")
    parser.add_argument("--count", type=int, default=20, help="takes per phrase")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "wake" / "real" / "positive")
    parser.add_argument("--model", type=Path, default=ROOT / "models" / "wake" / "vivi.onnx")
    parser.add_argument("--threshold", type=float, default=.5)
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    import sounddevice as sd
    import soundfile as sf
    from openwakeword.model import Model

    detector = Model(wakeword_models=[str(args.model)], inference_framework="onnx") if args.model.exists() else None
    args.out.mkdir(parents=True, exist_ok=True)
    chunks: queue.Queue[np.ndarray] = queue.Queue()
    results: dict[str, list[float]] = {key: [] for key in PHRASES}
    order = [key for _ in range(args.count) for key in PHRASES]

    def on_audio(data, frames, when, status):
        chunks.put(data[:, 0].copy())

    print(f"{len(order)} lượt thu. Sau mỗi lượt: Enter để giữ, x + Enter để bỏ và thu lại, q + Enter để dừng.\n")
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                        device=args.device, callback=on_audio):
        index = 0
        while index < len(order):
            key = order[index]
            print(f"[{index + 1}/{len(order)}] Nói: «{PHRASES[key]}»  ", end="", flush=True)
            for tick in ("3", "2", "1"):
                print(tick, end=" ", flush=True)
                time.sleep(.4)
            while not chunks.empty():
                chunks.get()
            print("— nói!", flush=True)
            audio = np.concatenate([chunks.get() for _ in range(int(args.seconds * RATE / CHUNK))])
            level = float(np.abs(audio).max()) / 32768
            score = peak_score(detector, audio) if detector else float("nan")
            verdict = "" if detector is None else ("✅ thức" if score >= args.threshold else "❌ bỏ sót")
            warning = "  (rất nhỏ, nói to hơn hoặc lại gần mic)" if level < .03 else ""
            answer = input(f"    điểm {score:.2f} {verdict}  âm lượng {level:.2f}{warning}  › ").strip().lower()
            if answer == "q":
                break
            if answer == "x":
                continue
            existing = len(list(args.out.glob(f"{args.speaker}-{key}-*.wav")))
            sf.write(args.out / f"{args.speaker}-{key}-{existing:03d}.wav", audio, RATE, subtype="PCM_16")
            results[key].append(score)
            index += 1

    if detector is not None:
        print()
        for key, scores in results.items():
            if scores:
                caught = sum(score >= args.threshold for score in scores)
                print(f"«{PHRASES[key]}»: bắt được {caught}/{len(scores)}  (điểm: {' '.join(f'{s:.2f}' for s in scores)})")
    print(f"Đã lưu vào {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
