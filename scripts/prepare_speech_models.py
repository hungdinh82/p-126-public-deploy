"""Download pinned speech artifacts during setup, never during inference."""
import argparse
from pathlib import Path

from huggingface_hub import snapshot_download, hf_hub_download

WHISPER_REV = "e37978b90ca9030d5170a5c07aadb050351a65bb"
WHISPER_CPP_REV = "5359861c739e955e79d9a303bcbc70fb988958b1"
TTS_REV = "c2bfbd67dc648cac455077333f7cf5c18a2e3bb4"


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
    print(f"Speech models ready: {root}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", choices=["transformers", "whisper_cpp"], default="transformers")
    args = parser.parse_args()
    prepare(args.root, args.engine)
