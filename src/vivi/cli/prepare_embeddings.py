from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.vivi.rag.embeddings.local import file_sha256


def main() -> None:
    parser = argparse.ArgumentParser(description="Download pinned ONNX E5 artifacts once; inference stays offline")
    parser.add_argument("--directory", type=Path, default=Path("models/multilingual-e5-small-int8"))
    parser.add_argument("--revision", default="761b726dd34fb83930e26aab4e9ac3899aa1fa78")
    parser.add_argument("--download-client", choices=("hub", "curl"), default="hub")
    args = parser.parse_args()
    repository = "Xenova/multilingual-e5-small"
    if args.download_client == "hub":
        from huggingface_hub import HfApi, hf_hub_download

        revision = HfApi().model_info(repository, revision=args.revision, timeout=15).sha
    else:
        metadata = subprocess.check_output([
            "curl", "-fsSL", "--connect-timeout", "10", "--max-time", "30",
            f"https://huggingface.co/api/models/{repository}/revision/{args.revision}",
        ], text=True)
        revision = json.loads(metadata)["sha"]
    args.directory.mkdir(parents=True, exist_ok=True)
    checksums = {}
    with tempfile.TemporaryDirectory(prefix="vivi-model-") as directory:
        for source, destination in [("tokenizer.json", "tokenizer.json"), ("onnx/model_quantized.onnx", "model_quantized.onnx")]:
            target = Path(directory) / destination
            if args.download_client == "hub":
                cached = hf_hub_download(repository, source, revision=revision)
                shutil.copyfile(cached, target)
            else:
                subprocess.run([
                    "curl", "-fsSL", "--retry", "2", "--connect-timeout", "10", "--max-time", "300",
                    f"https://huggingface.co/{repository}/resolve/{revision}/{source}", "-o", str(target),
                ], check=True)
            checksums[destination] = file_sha256(target)
        for name in checksums:
            shutil.copyfile(Path(directory) / name, args.directory / name)
    manifest = {
        "schema_version": 1,
        "model": "intfloat/multilingual-e5-small",
        "artifact_repository": repository,
        "revision": revision,
        "dimensions": 384,
        "quantization": "int8",
        "files": checksums,
    }
    path = args.directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
