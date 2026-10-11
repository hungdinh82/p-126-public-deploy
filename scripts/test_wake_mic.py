"""Try the wake-word model live with the laptop microphone.

    .venv-wake/bin/python scripts/test_wake_mic.py
    .venv-wake/bin/python scripts/test_wake_mic.py --threshold .7 --save data/wake/real

Say "ViVi ơi" or "Hey ViVi": the bar shows the score every 80 ms and a line is
printed on each wake. A rise that stays under the threshold prints its peak, so
a near miss (0.35) can be told from a call the model did not hear at all.
With --save, the 2 s around every wake is written to a WAV (useful as real test
clips, and to find out what caused a false wake).
Ctrl+C prints a summary.
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RATE = 16_000
CHUNK = 1280                # 80 ms, one openWakeWord frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=ROOT / "models" / "wake" / "vivi.onnx")
    parser.add_argument("--threshold", type=float, default=.5)
    parser.add_argument("--cooldown", type=float, default=2, help="seconds to ignore after a wake")
    parser.add_argument("--device", default=None, help="input device name or index (see --list)")
    parser.add_argument("--list", action="store_true", help="list audio devices and exit")
    parser.add_argument("--save", type=Path, help="folder to save 2 s of audio around each wake")
    args = parser.parse_args()

    import sounddevice as sd

    if args.list:
        print(sd.query_devices())
        return 0
    from openwakeword.model import Model

    detector = Model(wakeword_models=[str(args.model)], inference_framework="onnx")
    chunks: queue.Queue[np.ndarray] = queue.Queue()
    history: deque[np.ndarray] = deque(maxlen=int(2 * RATE / CHUNK))
    wakes, misses, peak, started, quiet_until = 0, 0, 0.0, time.time(), 0.0
    rise = 0.0                  # peak of the current rise that has not woken
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    def on_audio(data, frames, when, status):
        chunks.put(data[:, 0].copy())

    print(f"Model {args.model.name}, threshold {args.threshold}. Nói \"ViVi ơi\" hoặc \"Hey ViVi\". Ctrl+C để dừng.\n")
    try:
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                            device=args.device, callback=on_audio):
            def listen():
                audio = chunks.get()
                history.append(audio)
                return audio, float(next(iter(detector.predict(audio).values())))

            while True:
                audio, score = listen()
                peak = max(peak, score)
                level = int(np.sqrt(np.mean(audio.astype(np.float32) ** 2)) / 300)
                now = time.time()
                bar = "█" * int(score * 30)
                sys.stdout.write(f"\r  score {score:4.2f} |{bar:<30}|  mic {'▮' * min(level, 10):<10}")
                sys.stdout.flush()
                if score < args.threshold and now >= quiet_until:
                    if score >= .05:
                        rise = max(rise, score)
                    elif rise:
                        misses += 1
                        print(f"\r  · lên tới {rise:.2f}, chưa đủ ngưỡng {args.threshold}".ljust(70))
                        rise = 0.0
                if score >= args.threshold and now >= quiet_until:
                    rise = 0.0
                    wakes += 1
                    quiet_until = now + args.cooldown
                    stamp = time.strftime("%H:%M:%S")
                    line = f"\r  ✅ WAKE #{wakes} lúc {stamp}  score {score:.2f}"
                    if args.save:
                        import soundfile as sf

                        # Let the phrase finish before cutting the clip; the detector still hears it.
                        tail = [listen()[0] for _ in range(4)]
                        path = args.save / f"wake-{time.strftime('%Y%m%d-%H%M%S')}-{score:.2f}.wav"
                        sf.write(path, np.concatenate([*history, *tail]), RATE, subtype="PCM_16")
                        line += f"  → {path.name}"
                    print(f"{line:<70}\a")
    except KeyboardInterrupt:
        minutes = (time.time() - started) / 60
        print(f"\n\n{wakes} lần thức, {misses} lần nhích lên nhưng chưa đủ ngưỡng, "
              f"trong {minutes:.1f} phút; điểm cao nhất {peak:.2f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
