"""Resolve the ZeroTTS artifact selected at setup, without loading ONNX."""

import json
from pathlib import Path


def resolve_tts_model(base: Path, precision: str | None = None) -> tuple[Path, str]:
    if precision not in (None, "auto", "fp32", "fp16", "int8", "int8-gpu"):
        raise ValueError("TTS precision must be auto, fp32, fp16 (CUDA), int8 (CPU) or int8-gpu (TensorRT)")
    root = base.parent
    if precision in (None, "auto"):
        if base.name == "zerotts-fp16":
            precision = "fp16"
        elif base.name == "zerotts-int8-gpu":
            precision = "int8-gpu"
        elif base.name == "zerotts-int8":
            precision = "int8"
        elif base.name == "zerotts" and (root / "tts-profile.json").is_file():
            precision = json.loads((root / "tts-profile.json").read_text())["precision"]
        else:
            precision = "fp32"
    if precision == "fp32":
        return (root / "zerotts" if base.name in ("zerotts-int8", "zerotts-int8-gpu", "zerotts-fp16") else base), "fp32"
    if precision == "fp16":
        path = root / "zerotts-fp16"
        manifest = path / "quantization.json"
        if not manifest.is_file():
            raise FileNotFoundError("FP16 model missing; run setup --tts-precision fp16")
        data = json.loads(manifest.read_text())
        if data.get("format") != "float16-mixed" or data.get("execution_provider") != "CUDAExecutionProvider":
            raise ValueError("Unsupported ZeroTTS FP16 artifact")
        return path, "float16-mixed"
    if precision == "int8-gpu":
        path = root / "zerotts-int8-gpu"
        manifest = path / "quantization.json"
        if not manifest.is_file():
            raise FileNotFoundError("GPU INT8 model missing; run setup --tts-precision int8-gpu")
        data = json.loads(manifest.read_text())
        if data.get("format") != "int8-qdq-mixed" or data.get("execution_provider") != "TensorrtExecutionProvider":
            raise ValueError("Unsupported ZeroTTS GPU INT8 artifact")
        return path, "int8-qdq-mixed"
    if precision != "int8":
        raise ValueError("Invalid setup TTS precision")
    path = root / "zerotts-int8"
    manifest = path / "quantization.json"
    if not manifest.is_file():
        raise FileNotFoundError("INT8 model missing; run setup --tts-precision int8")
    data = json.loads(manifest.read_text())
    if data.get("format") != "int8-dynamic-mixed" or data.get("execution_provider") != "CPUExecutionProvider":
        raise ValueError("Unsupported ZeroTTS INT8 artifact")
    return path, "int8-dynamic-mixed"
