"""Download pinned speech artifacts during setup, never during inference."""
import argparse
import hashlib
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


def prepare(root: Path, engine: str):
    root.mkdir(parents=True, exist_ok=True)
    if engine == "whisper_cpp":
        hf_hub_download("ggerganov/whisper.cpp", filename="ggml-base.bin",
                        revision=WHISPER_CPP_REV, local_dir=root / "whisper")
    else:
        snapshot_download("openai/whisper-base", revision=WHISPER_REV,
                          local_dir=root / "whisper", allow_patterns=[
                              "*.json", "*.txt", "model.safetensors", "pytorch_model.bin"])
    snapshot_download("zeroweight-ai/ZeroTTS", revision=TTS_REV, local_dir=root / "zerotts",
                      allow_patterns=["*.json", "*.onnx", "*.data", "*.npy", "*.txt", "*.npz"])
    ensure_original_tts(root)
    print(f"Speech models ready: {root}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", choices=["transformers", "whisper_cpp"], default="transformers")
    args = parser.parse_args()
    prepare(args.root, args.engine)
