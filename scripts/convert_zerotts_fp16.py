"""Prepare a separate CUDA FP16 transformer variant; retain FP32 codec and IO."""

import argparse
import gc
import json
import shutil
import tempfile
from pathlib import Path

from scripts.quantize_zerotts import GRAPHS, artifact_hashes


def convert(source: Path, destination: Path):
    import onnx
    import onnxruntime as ort
    from onnxruntime.transformers.float16 import convert_float_to_float16

    if source.resolve() == destination.resolve():
        raise ValueError("FP16 output must be separate from the original model")
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("FP16 validation requires CUDA ONNX Runtime")
    settings = {
        "format": "float16-mixed",
        "execution_provider": "CUDAExecutionProvider",
        "ort_version": ort.__version__,
        "onnx_version": onnx.__version__,
        "keep_io_types": True,
        "codec_precision": "fp32",
        "source_sha256": artifact_hashes(source),
    }
    manifest_path = destination / "quantization.json"
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text())
        if all(previous.get(key) == value for key, value in settings.items()) and previous.get(
            "output_sha256"
        ) == artifact_hashes(destination):
            print(f"Reusing verified FP16 model: {destination}", flush=True)
            return previous
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".zerotts-fp16-", dir=destination.parent) as temp:
        stage = Path(temp) / "model"
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns(".cache", *GRAPHS))
        for name in GRAPHS:
            print(f"Converting {name}", flush=True)
            model = convert_float_to_float16(onnx.load(str(source / "onnx" / name)), keep_io_types=True)
            output = stage / "onnx" / name
            onnx.save(model, str(output))
            del model
            gc.collect()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            session = ort.InferenceSession(
                str(output), sess_options=options, providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
            )
            if session.get_providers()[0] != "CUDAExecutionProvider":
                raise RuntimeError(f"CUDA unavailable for {name}")
            del session
            gc.collect()
        manifest = {**settings, "graphs": list(GRAPHS), "output_sha256": artifact_hashes(stage)}
        (stage / "quantization.json").write_text(json.dumps(manifest, indent=2) + "\n")
        backup = Path(temp) / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    convert(args.source, args.destination)
