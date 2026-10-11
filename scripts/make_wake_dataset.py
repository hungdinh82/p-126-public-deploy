"""Build a local wake-word dataset for "ViVi ơi" and "Hey ViVi" with ZeroTTS.

Everything runs on this machine: ZeroTTS speaks the clips, cabin noise and
impulse responses are synthesized, and PhoWhisper (optional, --verify) rejects
clips that do not sound like their text. Re-running resumes where it stopped.

Output, all 16 kHz 16-bit mono WAV, under --out (default data/wake):

  clean/<split>/<kind>/        TTS clips, trimmed (kind: positive, negative, hard_negative)
  noise/<split>/<type>/        noise beds: engine, road, wind, hvac, rain, music, indicator, traffic, babble
                               (drop real recordings, any rate, into these folders and re-run)
  rir/<split>/                 car-cabin impulse responses
  augmented/<split>/<label>/   clips with reverb, noise and mic effects (label: positive, negative)
  openwakeword/vivi/            positive_train, positive_test, negative_train, negative_test (symlinks)
  manifest.jsonl               one row per clip with text, voice and augmentation

Speakers are split, not clips: test voices never appear in train.

    .venv/bin/python scripts/make_wake_dataset.py --positives 200 --negatives 200   # quick look
    .venv/bin/python scripts/make_wake_dataset.py --verify                          # full run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

ROOT = Path(__file__).resolve().parents[1]
RATE = 16_000
TEST_VOICES = ("kimoanh", "tiendat")

# Both phrases wake the same model; positives alternate between them. ZeroTTS
# reads "Hey" the Vietnamese way ("hây"), which is how drivers here say it too.
POSITIVE_TEXTS = [
    "Vi Vi ơi", "Vi Vi ơi.", "Vi Vi ơi!", "Vi Vi ơi,", "ViVi ơi", "Vi vi ơi",
    "Vi Vi ơi?", "Này, Vi Vi ơi", "À, Vi Vi ơi", "Ê Vi Vi ơi",
]
HEY_TEXTS = [
    "Hey Vi Vi", "Hey ViVi", "Hey Vi Vi.", "Hey, Vi Vi!", "Hây Vi Vi", "Hey Vi Vi ơi",
]
# Sounds close to the wake phrase, or says "ViVi" without calling her.
HARD_NEGATIVE_TEXTS = [
    "Vy ơi", "Vi ơi", "Chị Vy ơi", "Em Vy ơi", "Bé Vy ơi", "Bi Bi ơi", "Mi Mi ơi",
    "Li Li ơi", "Ni Ni ơi", "Di Di ơi", "Hi Hi ơi", "Thi Thi ơi", "Vinh ơi", "Vân ơi",
    "Vui vui", "Vi vu", "Vì vậy", "Vì vì", "Đi đi", "Đi đi ơi", "Vi vi", "Bí bí",
    "Ví vì ơi", "Vì vậy ơi", "Vi Vi đây", "Mình là Vi Vi", "Vi Vi đã bật điều hòa",
    "Vi Vi đang nghe", "Vi Vi chưa hiểu", "Tivi ơi", "Tivi", "Vivo", "Viva",
    "Uy ơi", "Uyên ơi", "Vy Vy à", "Vi Vi nhé",
    "Hey Siri", "Hey Google", "Hey Vy", "Hay vì vậy", "Thay vì vậy", "Hay quá", "Hay đấy",
    "Thầy ơi", "Hây da", "Hey", "Hey Bibi", "Hey Mimi", "Hay vì vì", "Hãy đi đi",
]
NEGATIVE_TEXTS = [
    "Bật điều hòa lên hai mươi hai độ", "Mở cửa sổ bên tài", "Đóng tất cả cửa sổ",
    "Tìm trạm sạc gần nhất", "Chỉ đường về nhà", "Tôi hơi lạnh", "Tăng âm lượng lên",
    "Hôm nay trời đẹp quá", "Anh ơi, rẽ phải ở ngã tư tới", "Em ơi, mình đi đâu đây",
    "Mẹ ơi, con đói rồi", "Bố ơi, sắp tới chưa", "Dừng lại đổ xăng nhé",
    "Đường này kẹt xe quá", "Mở bài hát khác đi", "Gọi điện cho mẹ", "Nhắn tin cho anh Nam",
    "Mấy giờ rồi nhỉ", "Còn bao xa nữa", "Đi chậm thôi", "Cẩn thận xe máy bên phải",
    "Tối nay ăn gì", "Đặt bàn nhà hàng lúc bảy giờ", "Pin còn bao nhiêu phần trăm",
    "Bật đèn pha", "Tắt nhạc đi", "Chuyển sang chế độ thể thao", "Ghế nóng quá",
    "Cho anh hỏi đường ra cao tốc", "Thời tiết ngày mai thế nào", "Ừ, được rồi",
    "Không, cảm ơn", "Vâng", "Dạ", "Ơi", "Alo", "Xin chào", "Tạm biệt nhé",
    "Chạy thêm năm cây số nữa", "Rẽ trái ở vòng xoay", "Quay đầu xe",
    "Cái này bao nhiêu tiền", "Mai mình đi Hà Nội", "Bạn có nghe không",
    "Vinfast VF8 đi được bao xa", "Mở cốp xe", "Khóa cửa lại", "Bật sưởi kính sau",
    "Hôm qua đội tuyển thắng hai không", "Con mèo nhà tôi rất lười",
]
NOISE_TYPES = ("engine", "road", "wind", "hvac", "rain", "music", "indicator", "traffic", "babble")
NOISE_WEIGHTS = (3, 4, 2, 3, 1, 3, 1, 1, 3)
WAKE_PATTERN = re.compile(r"\bv[iy]\s*v[iy]\b.*\bơi\b|\b(hey|hei|hây|hay|he|hê)\s*v[iy]\s*v[iy]\b")


def write(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.clip(audio, -1, 1), RATE, subtype="PCM_16")


def read(path: Path) -> np.ndarray:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    return audio if rate == RATE else resample(audio, RATE / rate)


def resample(audio: np.ndarray, ratio: float) -> np.ndarray:
    step = Fraction(ratio).limit_denominator(64)
    return signal.resample_poly(audio, step.numerator, step.denominator).astype(np.float32)


def filtered(audio: np.ndarray, kind: str, cutoff) -> np.ndarray:
    return signal.sosfilt(signal.butter(4, cutoff, kind, fs=RATE, output="sos"), audio).astype(np.float32)


def normalize(audio: np.ndarray, peak: float = .9) -> np.ndarray:
    top = float(np.max(np.abs(audio))) if audio.size else 0
    return audio * (peak / top) if top > 1e-6 else audio


def trim(audio: np.ndarray, margin: float = .08) -> np.ndarray:
    frame = RATE // 50
    energy = np.array([np.sqrt(np.mean(audio[i:i + frame] ** 2)) for i in range(0, len(audio), frame)])
    voiced = np.flatnonzero(energy > energy.max() * 10 ** (-38 / 20)) if energy.size else []
    if not len(voiced):
        return audio[:0]
    pad = int(margin * RATE)
    return audio[max(0, voiced[0] * frame - pad):min(len(audio), (voiced[-1] + 1) * frame + pad)]


# ── speakers ──────────────────────────────────────────────────────────────────

def speakers(model, blends: int, seed: int) -> dict[str, dict]:
    """Bundled voices, the VIVI pack, the model's free voice and blends of them.

    A blend is a weighted mix of two speaker latents; ZeroTTS speaks it as a new
    believable voice, which is how a handful of packs become dozens of speakers.
    """
    import io
    import zipfile

    rng = np.random.default_rng([seed, 7])
    named = {name: model.load_voice(name).emb for name in model.list_voices()}
    with zipfile.ZipFile(ROOT / "voices" / "VIVI.zip") as archive, \
            np.load(io.BytesIO(archive.read("VIVI/voice.npz"))) as data:
        named["VIVI"] = data["voice_emb"].astype(np.float32)
    test = [name for name in TEST_VOICES if name in named]
    train = [name for name in named if name not in test]
    pool = {name: {"split": "test" if name in test else "train", "emb": emb} for name, emb in named.items()}
    pool["free"] = {"split": "train", "emb": None}
    for split, names, count in (("train", [n for n in train if n != "VIVI"], blends), ("test", test, max(2, blends // 6))):
        for index in range(count):
            a, b = rng.choice(names, 2, replace=False)
            weight = float(rng.uniform(.2, .8))
            pool[f"{split}-mix{index:02d}-{a}-{b}"] = {
                "split": split, "emb": (weight * named[a] + (1 - weight) * named[b]).astype(np.float32),
                "weight": round(weight, 2),
            }
    return pool


# ── clean clips ───────────────────────────────────────────────────────────────

class Speaker:
    def __init__(self, verify: bool):
        from zerotts import ZeroTTS

        self.model = ZeroTTS.from_pretrained("zeroweight-ai/ZeroTTS", providers=["CPUExecutionProvider"])
        self.asr = None
        if verify:
            import torch
            from transformers import pipeline

            device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
            self.asr = pipeline("automatic-speech-recognition", model="vinai/PhoWhisper-medium", device=device)

    def say(self, text: str, emb, rng) -> np.ndarray:
        audio = self.model.synthesize(
            text, voice=emb,
            text_temperature=float(rng.uniform(.8, 1.1)),
            audio_temperature=float(rng.uniform(.6, 1.0)),
        ).reshape(-1)
        return trim(resample(audio, RATE / self.model.sample_rate))

    def heard(self, audio: np.ndarray) -> str:
        return self.asr({"raw": audio, "sampling_rate": RATE})["text"].lower()


def acceptable(audio: np.ndarray, kind: str) -> bool:
    seconds = len(audio) / RATE
    low, high = (.45, 2.6) if kind == "positive" else (.2, 8)
    return low <= seconds <= high and float(np.sqrt(np.mean(audio ** 2))) > 1e-3


def make_clean(args, speaker: Speaker, pool: dict, rows: list) -> None:
    queries = (json.loads(line).get("query") for file in sorted((ROOT / "eval").glob("*.jsonl")) for line in file.open())
    corpus = sorted({text for text in [*NEGATIVE_TEXTS, *queries] if isinstance(text, str) and 2 <= len(text) <= 120})
    hard_count = int(args.negatives * args.hard_share)
    jobs = [("positive", i, HEY_TEXTS if i % 2 else POSITIVE_TEXTS) for i in range(args.positives)]
    jobs += [("hard_negative", i, HARD_NEGATIVE_TEXTS) for i in range(hard_count)]
    jobs += [("negative", i, corpus) for i in range(args.negatives - hard_count)]
    test_names = [name for name, value in pool.items() if value["split"] == "test"]
    train_names = [name for name, value in pool.items() if value["split"] == "train"]
    started, made, rejected = time.time(), 0, 0
    for number, (kind, index, texts) in enumerate(jobs, 1):
        rng = np.random.default_rng([args.seed, ("positive", "hard_negative", "negative").index(kind), index])
        split = "test" if rng.random() < args.test_share else "train"
        name = str(rng.choice(test_names if split == "test" else train_names))
        text = str(rng.choice(texts))
        path = args.out / "clean" / split / kind / f"{kind}-{index:05d}.wav"
        row = {"path": str(path.relative_to(args.out)), "split": split, "kind": kind, "text": text, "voice": name}
        if path.exists():
            rows.append(row)
            continue
        for attempt in range(6):
            audio = speaker.say(text, pool[name]["emb"], rng)
            ok = acceptable(audio, kind)
            # Plain sentences cannot turn into the wake phrase; only check the ones that might.
            if ok and speaker.asr is not None and kind != "negative":
                said = bool(WAKE_PATTERN.search(speaker.heard(audio)))
                ok = said if kind == "positive" else not said
            if ok:
                break
            rejected += 1
        else:
            continue
        write(path, audio)
        rows.append(row)
        made += 1
        if made % 50 == 0 or number == len(jobs):
            rate = made / (time.time() - started)
            left = (len(jobs) - number) / rate / 60 if rate else 0
            print(f"  clean {number}/{len(jobs)}  made {made}  rejected {rejected}  ~{left:.0f} min left", flush=True)


# ── noise beds and impulse responses ──────────────────────────────────────────

def colored(n: int, exponent: float, rng) -> np.ndarray:
    """White noise shaped to 1/f^exponent power (1 pink, 2 brown)."""
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    spectrum *= 1 / np.maximum(freqs, 20) ** (exponent / 2)
    return normalize(np.fft.irfft(spectrum, n).astype(np.float32), .5)


def drift(n: int, rng, rate: float = .3, depth: float = .3) -> np.ndarray:
    """A slow random curve around 1, for speed and loudness changes."""
    points = max(4, int(n / RATE * rate) + 2)
    curve = np.interp(np.arange(n), np.linspace(0, n, points), rng.uniform(-1, 1, points))
    return (1 + depth * curve).astype(np.float32)


def tone(freq: np.ndarray, harmonics, rng) -> np.ndarray:
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    return sum(a * np.sin(k * phase + rng.uniform(0, 6.3)) for k, a in harmonics).astype(np.float32)


def envelope(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / RATE
    return (np.minimum(1, t / max(attack, 1e-4)) * np.exp(-t / decay)).astype(np.float32)


def add(bed: np.ndarray, sound: np.ndarray, at: int) -> None:
    end = min(len(bed), at + len(sound))
    if end > at:
        bed[at:end] += sound[:end - at]


def noise_bed(kind: str, seconds: float, rng, babble_clips: list[Path]) -> np.ndarray:
    n = int(seconds * RATE)
    if kind == "engine":
        rpm = rng.uniform(25, 70) * drift(n, rng, .2, .15)
        harmonics = [(k, rng.uniform(.3, 1) / k) for k in range(1, 9)]
        bed = tone(rpm, harmonics, rng) + .6 * filtered(colored(n, 2, rng), "lowpass", 300)
    elif kind == "road":
        bed = filtered(colored(n, 1.5, rng), "bandpass", [40, rng.uniform(700, 1600)]) * drift(n, rng, .8, .3)
        joint = filtered(rng.standard_normal(RATE // 4).astype(np.float32), "lowpass", 80) * envelope(RATE // 4, .002, .05)
        at = 0
        while (at := at + int(rng.uniform(.4, 3) * RATE)) < n:
            add(bed, joint * rng.uniform(1, 4), at)
    elif kind == "wind":
        gust = drift(n, rng, 1.5, .8).clip(.05)
        bed = filtered(rng.standard_normal(n).astype(np.float32), "bandpass", [150, rng.uniform(1500, 3500)]) * gust
    elif kind == "hvac":
        blade = np.full(n, rng.uniform(80, 250), np.float32)
        bed = filtered(rng.standard_normal(n).astype(np.float32), "bandpass", [300, rng.uniform(2500, 5000)])
        bed += tone(blade, [(1, .3), (2, .1)], rng) + tone(np.full(n, 50.0, np.float32), [(1, .1), (2, .1)], rng)
    elif kind == "rain":
        bed = .2 * filtered(rng.standard_normal(n).astype(np.float32), "highpass", 2000)
        drop = filtered(rng.standard_normal(400).astype(np.float32), "bandpass", [1000, 6000]) * envelope(400, .0005, .004)
        for at in rng.integers(0, n, int(seconds * rng.uniform(40, 400))):
            add(bed, drop * rng.uniform(.2, 1.5), int(at))
    elif kind == "music":
        bed = np.zeros(n, np.float32)
        beat = 60 / rng.uniform(70, 130)
        root = rng.uniform(110, 220)
        chords = [[0, 4, 7], [5, 9, 12], [7, 11, 14], [-3, 0, 4]]
        timbre = [(1, 1), (2, rng.uniform(.1, .5)), (3, rng.uniform(0, .3))]
        for bar, start in enumerate(np.arange(0, seconds, beat * 4)):
            length = int(beat * 4 * RATE)
            for step in chords[bar % 4]:
                note = tone(np.full(length, root * 2 ** (step / 12), np.float32), timbre, rng)
                add(bed, .3 * note * envelope(length, .02, beat * 3), int(start * RATE))
            for hit in range(4):
                kick = np.sin(2 * np.pi * 55 * np.arange(RATE // 5) / RATE).astype(np.float32) * envelope(RATE // 5, .001, .06)
                add(bed, kick, int((start + hit * beat) * RATE))
        bed = filtered(bed, "lowpass", rng.uniform(2500, 6000))
    elif kind == "indicator":
        bed = .3 * filtered(colored(n, 1.5, rng), "lowpass", 800)
        length = RATE // 25
        for index, start in enumerate(np.arange(rng.uniform(0, .75), seconds, .375)):
            click = np.sign(np.sin(2 * np.pi * (1900 if index % 2 == 0 else 1300) * np.arange(length) / RATE))
            add(bed, (click * envelope(length, .0005, .006)).astype(np.float32), int(start * RATE))
    elif kind == "traffic":
        bed = .3 * filtered(colored(n, 1.8, rng), "lowpass", 1200)
        for _ in range(int(seconds / 4)):
            at, length = int(rng.uniform(0, seconds) * RATE), int(rng.uniform(1.5, 4) * RATE)
            whoosh = filtered(rng.standard_normal(length).astype(np.float32), "bandpass", [100, 3000])
            add(bed, whoosh * signal.windows.gaussian(length, length / 5).astype(np.float32), at)
        for _ in range(int(seconds / 8) + 1):
            length = int(rng.uniform(.15, .8) * RATE)
            pitch = np.full(length, rng.uniform(380, 520), np.float32)
            honk = np.sign(tone(pitch, [(1, 1)], rng)) + np.sign(tone(pitch * 1.26, [(1, 1)], rng))
            add(bed, .3 * filtered(honk.astype(np.float32), "lowpass", 3000), int(rng.uniform(0, seconds) * RATE))
    elif kind == "babble":
        bed = np.zeros(n, np.float32)
        for _ in range(int(rng.integers(3, 8))):
            at = int(rng.uniform(-1, seconds) * RATE)
            while at < n and babble_clips:
                clip = normalize(read(babble_clips[int(rng.integers(len(babble_clips)))]), rng.uniform(.3, 1))
                add(bed, clip, max(0, at))
                at += len(clip) + int(rng.uniform(.1, .8) * RATE)
        bed = filtered(bed, "lowpass", 3500)
    else:
        raise ValueError(kind)
    return normalize(bed + 1e-4 * rng.standard_normal(n).astype(np.float32), .7)


def cabin_rir(rng) -> np.ndarray:
    """A short, damped room response: a car cabin is small and full of soft seats."""
    rt60 = rng.uniform(.04, .3)
    n = int(rt60 * 1.3 * RATE)
    t = np.arange(n) / RATE
    rir = filtered(rng.standard_normal(n).astype(np.float32), "lowpass", rng.uniform(2500, 7000))
    rir *= rng.uniform(.1, .4) * np.exp(-6.9 * t / rt60).astype(np.float32)
    rir[0] = 1
    for _ in range(int(rng.integers(4, 11))):
        rir[int(rng.uniform(.001, .01) * RATE)] += rng.uniform(.2, .7) * rng.choice([-1, 1])
    return normalize(rir, 1)


def make_noise(args) -> dict[str, dict[str, list[Path]]]:
    beds: dict[str, dict[str, list[Path]]] = {}
    for split, count in (("train", args.noise_beds), ("test", max(1, args.noise_beds // 3))):
        speech = sorted((args.out / "clean" / split / "negative").glob("*.wav"))
        beds[split] = {}
        for kind in NOISE_TYPES:
            for index in range(count):
                path = args.out / "noise" / split / kind / f"{kind}-{index:03d}.wav"
                if not path.exists():
                    rng = np.random.default_rng([args.seed, 11, NOISE_TYPES.index(kind), index, split == "test"])
                    write(path, noise_bed(kind, args.noise_seconds, rng, speech))
            # Real recordings dropped into the same folder are mixed in too.
            beds[split][kind] = sorted((args.out / "noise" / split / kind).glob("*.wav"))
        rir_count = args.rirs if split == "train" else max(4, args.rirs // 4)
        for index in range(rir_count):
            path = args.out / "rir" / split / f"rir-{index:03d}.wav"
            if not path.exists():
                write(path, cabin_rir(np.random.default_rng([args.seed, 13, index, split == "test"])))
    return beds


# ── augmentation ──────────────────────────────────────────────────────────────

class Mixer:
    def __init__(self, args, beds):
        self.beds = {split: {kind: [read(p) for p in paths] for kind, paths in kinds.items()} for split, kinds in beds.items()}
        self.rirs = {split: [read(p) for p in sorted((args.out / "rir" / split).glob("*.wav"))] for split in beds}
        self.weights = np.array(NOISE_WEIGHTS, float) / sum(NOISE_WEIGHTS)

    def noise(self, split: str, n: int, rng) -> tuple[np.ndarray, list[str]]:
        kinds = list(rng.choice(NOISE_TYPES, int(rng.integers(1, 4)), replace=False, p=self.weights))
        mix = np.zeros(n, np.float32)
        for kind in kinds:
            bed = self.beds[split][kind][int(rng.integers(len(self.beds[split][kind])))]
            start = int(rng.integers(0, max(1, len(bed) - n)))
            part = np.resize(bed[start:start + n], n)
            mix += part * rng.uniform(.3, 1) / (np.sqrt(np.mean(part ** 2)) + 1e-6)
        return mix, kinds

    def mic(self, audio: np.ndarray, rng, info: dict) -> np.ndarray:
        if rng.random() < .8:
            info["highpass"] = round(float(rng.uniform(60, 200)))
            audio = filtered(audio, "highpass", info["highpass"])
        if rng.random() < .4:
            info["lowpass"] = round(float(rng.uniform(3400, 7600)))
            audio = filtered(audio, "lowpass", info["lowpass"])
        audio = normalize(audio, float(rng.uniform(.05, .95)))
        if rng.random() < .04:
            info["clipped"] = True
            audio = np.clip(audio * rng.uniform(1.5, 4), -.9, .9)
        return audio

    def speech(self, clip: np.ndarray, split: str, positive: bool, rng) -> tuple[np.ndarray, dict]:
        info: dict = {}
        if rng.random() < .6:
            info["speed"] = round(float(rng.uniform(.88, 1.12)), 3)
            clip = resample(clip, 1 / info["speed"])
        if rng.random() < .7:
            rir = self.rirs[split][int(rng.integers(len(self.rirs[split])))]
            loud = np.sqrt(np.mean(clip ** 2))
            clip = signal.fftconvolve(clip, rir)[:len(clip) + len(rir) // 2].astype(np.float32)
            clip *= loud / (np.sqrt(np.mean(clip ** 2)) + 1e-9)
            info["reverb"] = True
        # The wake phrase ends near the end of the window, as the detector sees it live.
        before = rng.uniform(.3, 1.2)
        after = rng.uniform(.1, .4) if positive else rng.uniform(.1, 1)
        audio = np.concatenate([np.zeros(int(before * RATE), np.float32), clip, np.zeros(int(after * RATE), np.float32)])
        if rng.random() < .92:
            noise, info["noise"] = self.noise(split, len(audio), rng)
            snr = float(rng.triangular(-3, 10, 25))
            speech_power = np.mean(clip ** 2) + 1e-9
            audio = audio + noise * np.sqrt(speech_power / (np.mean(noise ** 2) + 1e-9) / 10 ** (snr / 10))
            info["snr_db"] = round(snr, 1)
        return self.mic(audio, rng, info), info

    def silence(self, split: str, rng) -> tuple[np.ndarray, dict]:
        noise, kinds = self.noise(split, int(rng.uniform(1, 4) * RATE), rng)
        info = {"noise": kinds}
        return self.mic(noise, rng, info), info


def make_augmented(args, beds, rows: list) -> None:
    mixer = Mixer(args, beds)
    clean = [row for row in rows if row["path"].startswith("clean/")]
    made = 0
    for row in clean:
        positive = row["kind"] == "positive"
        clip = read(args.out / row["path"])
        for copy in range(args.copies if positive else max(1, args.copies - 1)):
            label = "positive" if positive else "negative"
            path = args.out / "augmented" / row["split"] / label / f"{Path(row['path']).stem}-{copy}.wav"
            rng = np.random.default_rng([args.seed, 17, len(row["kind"]), int(path.stem.split("-")[-2]), copy])
            audio, info = mixer.speech(clip, row["split"], positive, rng)
            if not path.exists():
                write(path, audio)
                made += 1
            rows.append({**row, "path": str(path.relative_to(args.out)), "label": label, "source": row["path"], **info})
    for index in range(args.noise_only):
        split = "test" if index % int(1 / args.test_share) == 0 else "train"
        rng = np.random.default_rng([args.seed, 19, index])
        audio, info = mixer.silence(split, rng)
        path = args.out / "augmented" / split / "negative" / f"noise-{index:05d}.wav"
        if not path.exists():
            write(path, audio)
            made += 1
        rows.append({"path": str(path.relative_to(args.out)), "split": split, "kind": "noise", "label": "negative", **info})
    print(f"  augmented {made} new clips")


def link_openwakeword(args) -> None:
    """openWakeWord's trainer reads clips from <output_dir>/<model_name>/{positive,negative}_{train,test}."""
    target = args.out / "openwakeword" / "vivi"
    for label in ("positive", "negative"):
        for split in ("train", "test"):
            folder = target / f"{label}_{split}"
            folder.mkdir(parents=True, exist_ok=True)
            kinds = ["positive"] if label == "positive" else ["negative", "hard_negative"]
            for kind in kinds:
                for clip in (args.out / "clean" / split / kind).glob("*.wav"):
                    link = folder / clip.name
                    if not link.exists():
                        link.symlink_to(clip.resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "wake")
    parser.add_argument("--positives", type=int, default=4000)
    parser.add_argument("--negatives", type=int, default=4000, help="speech negatives, hard ones included")
    parser.add_argument("--hard-share", type=float, default=.45, help="part of the negatives that sound like the wake phrase")
    parser.add_argument("--noise-only", type=int, default=2000, help="negatives with no speech at all")
    parser.add_argument("--copies", type=int, default=3, help="augmented copies of each positive (negatives get one less)")
    parser.add_argument("--blends", type=int, default=40, help="extra train voices mixed from two packs")
    parser.add_argument("--noise-beds", type=int, default=6, help="train beds per noise type")
    parser.add_argument("--noise-seconds", type=float, default=20)
    parser.add_argument("--rirs", type=int, default=80)
    parser.add_argument("--test-share", type=float, default=.15)
    parser.add_argument("--verify", action="store_true", help="drop positives PhoWhisper does not hear as the wake phrase, and hard negatives it does")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()

    rows: list[dict] = []
    print("1/4 Loading ZeroTTS" + (" and PhoWhisper" if args.verify else ""), flush=True)
    speaker = Speaker(args.verify)
    pool = speakers(speaker.model, args.blends, args.seed)
    print(f"    {sum(v['split'] == 'train' for v in pool.values())} train voices, "
          f"{sum(v['split'] == 'test' for v in pool.values())} test voices", flush=True)
    print("2/4 Speaking clean clips", flush=True)
    make_clean(args, speaker, pool, rows)
    print("3/4 Building noise beds and cabin impulse responses", flush=True)
    beds = make_noise(args)
    print("4/4 Augmenting", flush=True)
    make_augmented(args, beds, rows)
    link_openwakeword(args)

    with (args.out / "manifest.jsonl").open("w") as manifest:
        for row in rows:
            manifest.write(json.dumps(row, ensure_ascii=False) + "\n")
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['path'].split('/')[0]}/{row['split']}/{row.get('label', row['kind'])}"
        counts[key] = counts.get(key, 0) + 1
    for key, count in sorted(counts.items()):
        print(f"    {key:32} {count}")
    print(f"Done → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
