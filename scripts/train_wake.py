"""Train the "ViVi ơi" / "Hey ViVi" wake-word model on the dataset from make_wake_dataset.py.

openWakeWord's frozen front end (melspectrogram + Google speech embedding) turns
audio into 96-dim frames every 80 ms; only a small classifier over 16 frames
(1.28 s) is trained here. The exported ONNX has openWakeWord's own model shape,
so the same file runs in openwakeword.Model (laptop, Jetson) and in
onnxruntime-web behind the same front end (browser).

Runs in the separate Python 3.11 env:

    uv venv .venv-wake --python 3.11
    uv pip install --python .venv-wake/bin/python openwakeword torch onnx onnxruntime soundfile scipy huggingface_hub
    .venv-wake/bin/python scripts/train_wake.py

Negatives come from three places: the dataset's own negatives (Vietnamese speech,
confusable phrases, cabin noise), openWakeWord's precomputed ACAV100M features
(~2,000 h of general audio, --features, optional but the main
defence against false wakes) and its validation features, which only measure
false wakes per hour and are never trained on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
RATE = 16_000
FRAMES = 16                 # classifier input: 16 embedding frames = 1.28 s
FRAME_SECONDS = .08
MIN_SAMPLES = 2 * RATE + 1280   # audio for two 16-frame windows
ACAV = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VALIDATION = "validation_set_features.npy"


# ── features ──────────────────────────────────────────────────────────────────

def clip_windows(features, path: Path, positive: bool, stride: int, rng) -> np.ndarray:
    audio, rate = sf.read(path, dtype="int16")
    if rate != RATE:
        raise ValueError(f"{path} is {rate} Hz; the dataset should be {RATE} Hz")
    return audio_windows(features, audio, positive, stride, rng)


def audio_windows(features, audio: np.ndarray, positive: bool, stride: int, rng) -> np.ndarray:
    if len(audio) < MIN_SAMPLES:
        # Quiet dither, not digital zeros: a live microphone never delivers exact silence.
        pad = rng.normal(0, 8, MIN_SAMPLES - len(audio)).astype(np.int16)
        audio = np.concatenate([pad, audio])
    frames = features._get_embeddings(audio)
    windows = np.lib.stride_tricks.sliding_window_view(frames, (FRAMES, frames.shape[1]))[:, 0]
    # A positive ends with the wake phrase, so only the last windows hold all of it.
    return windows[-2:] if positive else windows[::stride]


def build_features(args) -> dict[str, np.ndarray]:
    from openwakeword.utils import AudioFeatures

    cache = args.data / "features"
    cache.mkdir(exist_ok=True)
    features = AudioFeatures(ncpu=args.cpus)
    sets = {}
    for split in ("train", "test"):
        for label in ("positive", "negative"):
            path = cache / f"{split}_{label}.npy"
            name = f"{split}_{label}"
            if path.exists():
                sets[name] = np.load(path, mmap_mode="r")
                continue
            clips = sorted((args.data / "augmented" / split / label).glob("*.wav"))
            if not clips:
                sys.exit(f"No clips in {args.data / 'augmented' / split / label}; run make_wake_dataset.py first")
            rng = np.random.default_rng(len(clips))
            started, windows = time.time(), []
            for index, clip in enumerate(clips, 1):
                windows.append(clip_windows(features, clip, label == "positive", args.negative_stride, rng))
                if index % 1000 == 0 or index == len(clips):
                    print(f"  {name}: {index}/{len(clips)} clips, {time.time() - started:.0f}s", flush=True)
            sets[name] = np.concatenate(windows).astype(np.float16)
            np.save(path, sets[name])
    return sets


def real_features(args) -> dict[str, np.ndarray]:
    """Real recorded calls (record_wake.py): every fifth take of each speaker and
    phrase is kept for testing, the rest is mixed with the dataset's cabin noise and
    reverb --real-copies times, plus once as recorded."""
    from types import SimpleNamespace

    from openwakeword.utils import AudioFeatures

    sys.path.insert(0, str(ROOT / "scripts"))
    import make_wake_dataset as dataset

    clips = sorted(args.real.glob("*.wav"))
    if not clips:
        sys.exit(f"No recordings in {args.real}; run record_wake.py first")
    beds = {"train": {kind: sorted((args.data / "noise" / "train" / kind).glob("*.wav")) for kind in dataset.NOISE_TYPES}}
    mixer = dataset.Mixer(SimpleNamespace(out=args.data), beds)
    features = AudioFeatures(ncpu=args.cpus)
    rng = np.random.default_rng(args.seed)
    found = {"real_train": [], "real_test": []}
    for clip in clips:
        audio = dataset.read(clip)
        end = voiced_end(audio)
        if end is None:
            continue
        # As recorded, cut a little after the phrase: the detector fires right as it ends.
        natural = audio[:min(len(audio), end + int(.25 * RATE))]
        held_out = int(clip.stem.rsplit("-", 1)[-1]) % 5 == 4
        variants = [natural]
        if not held_out:
            phrase = dataset.trim(audio[:end + int(.1 * RATE)])
            variants += [mixer.speech(phrase, "train", True, rng)[0] for _ in range(args.real_copies)]
        for variant in variants:
            pcm = (np.clip(variant, -1, 1) * 32767).astype(np.int16)
            found["real_test" if held_out else "real_train"].append(audio_windows(features, pcm, True, 1, rng))
    sets = {name: np.concatenate(windows).astype(np.float16) for name, windows in found.items() if windows}
    print(f"  real: {len(clips)} recordings → {len(sets.get('real_train', []))} train windows, "
          f"{len(sets.get('real_test', [])) // 2} test takes", flush=True)
    return sets


def voiced_end(audio: np.ndarray) -> int | None:
    frame = RATE // 50
    energy = np.array([np.sqrt(np.mean(audio[i:i + frame] ** 2)) for i in range(0, len(audio), frame)])
    voiced = np.flatnonzero(energy > energy.max() * 10 ** (-30 / 20)) if energy.size and energy.max() > 1e-3 else []
    return (int(voiced[-1]) + 1) * frame if len(voiced) else None


# ── model ─────────────────────────────────────────────────────────────────────

def make_model(width: int, blocks: int):
    import torch
    from torch import nn

    class WakeNet(nn.Module):
        """openWakeWord's DNN head: flatten, a few LayerNorm/ReLU layers, sigmoid."""

        def __init__(self):
            super().__init__()
            layers = [nn.Flatten(), nn.Linear(FRAMES * 96, width), nn.LayerNorm(width), nn.ReLU()]
            for _ in range(blocks):
                layers += [nn.Dropout(.2), nn.Linear(width, width), nn.LayerNorm(width), nn.ReLU()]
            self.body = nn.Sequential(*layers, nn.Linear(width, 1))

        def forward(self, x):
            return torch.sigmoid(self.body(x))

        def logits(self, x):
            return self.body(x)

    return WakeNet()


def predict(model, windows, device, batch: int = 8192) -> np.ndarray:
    import torch

    model.eval()
    scores = []
    with torch.no_grad():
        for start in range(0, len(windows), batch):
            chunk = torch.from_numpy(np.asarray(windows[start:start + batch], dtype=np.float32)).to(device)
            scores.append(model(chunk).cpu().numpy().reshape(-1))
    model.train()
    return np.concatenate(scores) if scores else np.zeros(0)


def stream_windows(frames: np.ndarray) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(frames, (FRAMES, frames.shape[1]))[:, 0]


def false_wakes_per_hour(scores: np.ndarray, threshold: float, hours: float) -> float:
    """Count wakes the way a live detector would: one per crossing, then 2 s of quiet."""
    wakes, cooldown = 0, int(2 / FRAME_SECONDS)
    index, hits = 0, np.flatnonzero(scores >= threshold)
    for hit in hits:
        if hit >= index:
            wakes += 1
            index = hit + cooldown
    return wakes / hours


def evaluate(model, sets, validation, device) -> dict:
    positive = predict(model, sets["test_positive"], device)
    negative = predict(model, sets["test_negative"], device)
    stream = predict(model, stream_windows(validation), device) if validation is not None else None
    real = predict(model, sets["real_test"], device) if "real_test" in sets else None
    hours = len(validation) * FRAME_SECONDS / 3600 if validation is not None else 0
    report = {}
    for threshold in (.3, .4, .5, .6, .7, .8, .9):
        # A test clip contributes two windows; it counts as heard if either fires.
        heard = positive.reshape(-1, 2).max(axis=1) >= threshold
        report[threshold] = {
            "recall": float(heard.mean()),
            "test_false_rate": float((negative >= threshold).mean()),
            "false_wakes_per_hour": false_wakes_per_hour(stream, threshold, hours) if stream is not None else None,
        }
        if real is not None:
            report[threshold]["real_recall"] = float((real.reshape(-1, 2).max(axis=1) >= threshold).mean())
    return report


# ── training ──────────────────────────────────────────────────────────────────

def batches(sets, acav, mined, args, rng):
    """Each batch mixes positives, the dataset's negatives, general ACAV audio and,
    after mining, the ACAV windows the model last mistook for the wake phrase."""
    positive, negative = sets["train_positive"], sets["train_negative"]
    while True:
        parts = [positive[np.sort(rng.integers(0, len(positive), args.batch))],
                 negative[np.sort(rng.integers(0, len(negative), args.batch))]]
        labels = [np.ones(args.batch), np.zeros(args.batch)]
        if acav is not None:
            # Scattered rows, not a slice: neighbouring rows are windows of the same recording.
            parts.append(acav[np.sort(rng.integers(0, len(acav), args.batch * 2))])
            labels.append(np.zeros(args.batch * 2))
        if mined is not None:
            parts.append(mined[rng.integers(0, len(mined), args.batch)])
            labels.append(np.zeros(args.batch))
        if "real_train" in sets:
            parts.append(sets["real_train"][rng.integers(0, len(sets["real_train"]), args.batch // 2)])
            labels.append(np.ones(args.batch // 2))
        yield np.concatenate(parts).astype(np.float32), np.concatenate(labels).astype(np.float32)


def score(report: dict, target: float) -> float:
    """Best recall at threshold .5 among checkpoints that stay under the false-wake target.
    Real recordings, when there are any, count for more than synthetic ones."""
    row = report[.5]
    false = row["false_wakes_per_hour"] if row["false_wakes_per_hour"] is not None else row["test_false_rate"] * 100
    recall = row["recall"] if "real_recall" not in row else .3 * row["recall"] + .7 * row["real_recall"]
    return recall - (10 * (false - target) if false > target else 0)


def device_name() -> str:
    import torch

    return "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"


def train(args, sets, acav, validation, model=None, mined=None, steps=None, lr=None):
    import torch

    device = device_name()
    steps, lr = steps or args.steps, lr or args.lr
    torch.manual_seed(args.seed)
    model = (model or make_model(args.width, args.blocks)).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-2)
    schedule = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=lr, total_steps=steps, pct_start=.1)
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="none")
    rng = np.random.default_rng(args.seed)
    best, best_state, started = -1e9, None, time.time()
    print(f"Training on {device}: {len(sets['train_positive'])} positive and "
          f"{len(sets['train_negative'])} negative windows" + (f", {len(acav)} ACAV windows" if acav is not None else "")
          + (f", {len(mined)} mined windows" if mined is not None else ""))
    for step, (x, y) in enumerate(batches(sets, acav, mined, args, rng), 1):
        x, y = torch.from_numpy(x).to(device), torch.from_numpy(y).to(device)
        # False wakes cost more as training goes on, once positives are learned.
        negative_weight = 1 + (args.negative_weight - 1) * min(1, step / (steps * .7))
        weights = torch.where(y > 0, 1.0, negative_weight)
        loss = (loss_fn(model.logits(x).reshape(-1), y) * weights).mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        schedule.step()
        if step % args.eval_every == 0 or step == steps:
            report = evaluate(model, sets, validation, device)
            row = report[.5]
            current = score(report, args.target)
            mark = ""
            if current > best:
                best, best_state, mark = current, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}, "  *"
            false = row["false_wakes_per_hour"]
            real = f"real {row['real_recall']:.3f}  " if "real_recall" in row else ""
            print(f"  step {step:6}  loss {loss.item():.4f}  recall {row['recall']:.3f}  {real}"
                  f"test false {row['test_false_rate']:.4f}  "
                  f"false/h {'-' if false is None else f'{false:.2f}'}  {time.time() - started:.0f}s{mark}", flush=True)
        if step >= steps:
            break
    model.load_state_dict(best_state)
    return model.cpu()


def mine(model, acav, previous, count: int):
    """Score every ACAV window and keep the ones the model most wants to wake on."""
    import torch

    device = device_name()
    model = model.to(device).eval()
    scores = np.zeros(len(acav), np.float32)
    started = time.time()
    with torch.no_grad():
        for start in range(0, len(acav), 65536):
            chunk = torch.from_numpy(np.asarray(acav[start:start + 65536], dtype=np.float32)).to(device)
            scores[start:start + len(chunk)] = model(chunk).cpu().numpy().reshape(-1)
    top = np.sort(np.argsort(scores)[-count:])
    print(f"    mined {count} windows from {len(acav)} in {time.time() - started:.0f}s; "
          f"{int((scores >= .5).sum())} scored ≥ .5, lowest kept {scores[top].min():.3f}", flush=True)
    found = np.asarray(acav[top], dtype=np.float16)
    model.train()
    return found if previous is None else np.concatenate([previous, found])


def export(model, path: Path) -> None:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    torch.onnx.export(model, torch.zeros(1, FRAMES, 96), path, input_names=["x"], output_names=["score"],
                      dynamic_axes={"x": {0: "batch"}, "score": {0: "batch"}}, opset_version=13, dynamo=False)


def check_stream(path: Path, clips: list[Path]) -> list[float]:
    """Run the exported file through openwakeword.Model exactly as the Jetson will."""
    from openwakeword.model import Model

    detector = Model(wakeword_models=[str(path)], inference_framework="onnx")
    peaks = []
    for clip in clips:
        detector.reset()
        audio = sf.read(clip, dtype="int16")[0]
        audio = np.concatenate([np.zeros(RATE, np.int16), audio, np.zeros(RATE // 2, np.int16)])
        scores = [next(iter(detector.predict(audio[i:i + 1280]).values())) for i in range(0, len(audio) - 1279, 1280)]
        peaks.append(float(max(scores)))
    return peaks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "wake")
    parser.add_argument("--features", type=Path, default=ROOT / "data" / "wake" / "oww_features",
                        help="where the ACAV100M and validation feature files are")
    parser.add_argument("--out", type=Path, default=ROOT / "models" / "wake" / "vivi.onnx")
    parser.add_argument("--steps", type=int, default=30_000)
    parser.add_argument("--batch", type=int, default=256, help="positives per batch; negatives match it, ACAV doubles it")
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--negative-weight", type=float, default=10, help="final weight of a negative in the loss")
    parser.add_argument("--negative-stride", type=int, default=2, help="keep every Nth window of a negative clip")
    # The validation features are English far-field audio, harsher than a cabin, so the
    # default trades a few of those false wakes for catching more real calls.
    parser.add_argument("--target", type=float, default=2, help="false wakes per hour the best checkpoint must stay under")
    parser.add_argument("--real", type=Path, help="folder of real recorded calls (record_wake.py)")
    parser.add_argument("--real-copies", type=int, default=30, help="noisy copies of each real training take")
    parser.add_argument("--eval-every", type=int, default=2000)
    parser.add_argument("--mine-rounds", type=int, default=3, help="rounds of retraining on ACAV windows the model gets wrong")
    parser.add_argument("--mine-count", type=int, default=50_000, help="ACAV windows added per mining round")
    parser.add_argument("--mine-steps", type=int, default=10_000, help="training steps after each mining round")
    parser.add_argument("--no-acav", action="store_true", help="train without the 17 GB ACAV100M features")
    parser.add_argument("--cpus", type=int, default=8)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()

    print("1/4 Features", flush=True)
    sets = build_features(args)
    if args.real:
        sets.update(real_features(args))
    acav_path, validation_path = args.features / ACAV, args.features / VALIDATION
    acav = None
    if not args.no_acav:
        if not acav_path.exists():
            sys.exit(f"Missing {acav_path}. Download it (see the docstring) or pass --no-acav for a quick model.")
        acav = np.load(acav_path, mmap_mode="r")
    validation = held_out = None
    if validation_path.exists():
        # Checkpoints are picked on the first half; only the second half is reported,
        # so the false-wake figure is not tuned to the data it is measured on.
        full = np.load(validation_path).astype(np.float32)
        validation, held_out = full[:len(full) // 2], full[len(full) // 2:]
    else:
        print(f"  {validation_path} missing: false wakes per hour will not be measured")

    print("2/4 Training", flush=True)
    model = train(args, sets, acav, validation)
    mined = None
    for round_ in range(1, args.mine_rounds + 1 if acav is not None else 1):
        print(f"  mining round {round_}/{args.mine_rounds}", flush=True)
        mined = mine(model, acav, mined, args.mine_count)
        model = train(args, sets, acav, validation, model=model, mined=mined, steps=args.mine_steps, lr=args.lr / 3)

    print("3/4 Exporting", flush=True)
    export(model, args.out)
    report = evaluate(model, sets, held_out, "cpu")
    print("    on the held-out half of the validation features:")
    print("    threshold  recall  real    test-false  false/h")
    for threshold, row in report.items():
        false = row["false_wakes_per_hour"]
        real = f"{row['real_recall']:.3f}" if "real_recall" in row else "  -  "
        print(f"    {threshold:9.1f}  {row['recall']:.3f}   {real}   {row['test_false_rate']:.4f}    "
              f"{'-' if false is None else f'{false:.2f}'}")

    print("4/4 Checking the ONNX file through openwakeword.Model", flush=True)
    clean = args.data / "clean" / "test"
    peaks = {kind: check_stream(args.out, sorted((clean / kind).glob("*.wav"))[:40]) for kind in ("positive", "hard_negative")}
    for kind, values in peaks.items():
        print(f"    {kind:14} peak score median {np.median(values):.2f}  min {min(values):.2f}  max {max(values):.2f}")
    args.out.with_suffix(".json").write_text(json.dumps({
        "phrases": ["ViVi ơi", "Hey ViVi"], "input": [1, FRAMES, 96], "acav": acav is not None,
        "steps": args.steps, "negative_weight": args.negative_weight, "mine_rounds": args.mine_rounds,
        "real": str(args.real) if args.real else None, "report": {str(k): v for k, v in report.items()},
    }, ensure_ascii=False, indent=2))
    print(f"Done → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
