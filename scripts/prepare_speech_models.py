"""Download pinned speech artifacts during setup, never during inference."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

WHISPER_REV = "e37978b90ca9030d5170a5c07aadb050351a65bb"
WHISPER_CPP_REV = "5359861c739e955e79d9a303bcbc70fb988958b1"
TTS_REV = "c2bfbd67dc648cac455077333f7cf5c18a2e3bb4"
TTS_FRAME_SHA256 = "3c4540ef4e69dcf604dc6f60f6f99f119189694ff5f31113def20406e274f025"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_original_tts(root):
    # A prior compatibility patch must not be reused by the new profile.
    path = root / "zerotts" / "onnx" / "local_frame_decode.onnx"
    if not path.is_file() or sha256(path) != TTS_FRAME_SHA256:
        hf_hub_download("zeroweight-ai/ZeroTTS", filename="onnx/local_frame_decode.onnx",
                        revision=TTS_REV, local_dir=root / "zerotts", force_download=True)
    if sha256(path) != TTS_FRAME_SHA256:
        raise RuntimeError("Original ZeroTTS frame decoder SHA256 mismatch")


def prepare(root: Path, engine: str, tts_precision: str = "fp32", local_only: bool = False,
            calibration_texts: Path | None = None, activate: bool = True):
    if tts_precision not in ("fp32", "fp16", "int8", "int8-gpu"):
        raise ValueError("TTS precision must be fp32, fp16 (CUDA), int8 (CPU) or int8-gpu")
    root.mkdir(parents=True, exist_ok=True)
    if local_only:
        if not (root / "whisper").is_dir():
            raise FileNotFoundError("Whisper model missing; run setup online first")
        if sha256(root / "zerotts/onnx/local_frame_decode.onnx") != TTS_FRAME_SHA256:
            raise RuntimeError("Original ZeroTTS frame decoder SHA256 mismatch")
    elif engine == "whisper_cpp":
        hf_hub_download("ggerganov/whisper.cpp", filename="ggml-base.bin",
                        revision=WHISPER_CPP_REV, local_dir=root / "whisper")
    else:
        snapshot_download("openai/whisper-base", revision=WHISPER_REV,
                          local_dir=root / "whisper", allow_patterns=[
                              "*.json", "*.txt", "model.safetensors", "pytorch_model.bin"])
    if not local_only:
        snapshot_download("zeroweight-ai/ZeroTTS", revision=TTS_REV, local_dir=root / "zerotts",
                          allow_patterns=["*.json", "*.onnx", "*.data", "*.npy", "*.txt", "*.npz"])
        ensure_original_tts(root)
    if tts_precision == "fp16":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.convert_zerotts_fp16 import convert
        convert(root / "zerotts", root / "zerotts-fp16")
    elif tts_precision == "int8":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.quantize_zerotts import quantize_zerotts
        quantize_zerotts(root / "zerotts", root / "zerotts-int8", TTS_REV)
    elif tts_precision == "int8-gpu":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.quantize_zerotts_gpu import CORPUS, quantize_zerotts_gpu
        corpus = tuple(line.strip() for line in calibration_texts.read_text().splitlines() if line.strip()) \
            if calibration_texts else CORPUS
        quantize_zerotts_gpu(root / "zerotts", root / "zerotts-int8-gpu", TTS_REV, corpus=corpus)
    if activate:
        select_profile(root, tts_precision)
    print(f"Speech models ready: {root}", flush=True)


def select_profile(root: Path, precision: str):
    # Xavier setup defers this until real TensorRT inference/inspection passes.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from jetson_ai.tts_profile import resolve_tts_model
    path, _ = resolve_tts_model(root / "zerotts", precision)
    if not path.is_dir():
        raise FileNotFoundError(f"TTS artifact missing: {path}")
    profile = root / "tts-profile.json"
    temporary = profile.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"precision": precision}) + "\n")
    temporary.replace(profile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", choices=["transformers", "whisper_cpp"], default="transformers")
    parser.add_argument("--tts-precision", choices=["fp32", "fp16", "int8", "int8-gpu"], default="int8")
    parser.add_argument("--calibration-texts", type=Path, help="UTF-8 calibration corpus, one sentence per line")
    parser.add_argument("--defer-selection", action="store_true", help="Prepare only; select after hardware smoke")
    parser.add_argument("--select-only", action="store_true", help="Select an already prepared artifact")
    parser.add_argument("--local-only", action="store_true", help="Use already downloaded models without network")
    args = parser.parse_args()
    if args.select_only:
        select_profile(args.root, args.tts_precision)
    else:
        prepare(args.root, args.engine, args.tts_precision, args.local_only, args.calibration_texts,
                activate=not args.defer_selection)
