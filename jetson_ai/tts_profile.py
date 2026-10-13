"""Resolve the ZeroTTS artifact selected at setup, without loading ONNX."""

import json
from pathlib import Path


def resolve_tts_model(base: Path, precision: str | None = None) -> tuple[Path, str]:
    if precision not in (None, "auto", "fp32", "int8"):
        raise ValueError("TTS precision must be auto, fp32 or int8")
    root = base.parent
    if precision in (None, "auto"):
        if base.name == "zerotts-int8":
            precision = "int8"
        elif base.name == "zerotts" and (root / "tts-profile.json").is_file():
            precision = json.loads((root / "tts-profile.json").read_text())["precision"]
        else:
            precision = "fp32"
    if precision == "fp32":
        return (root / "zerotts" if base.name == "zerotts-int8" else base), "fp32"
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
