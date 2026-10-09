"""Read-only CUDA/runtime checks, with an optional setup manifest."""

import argparse
import ctypes
import hashlib
import json
import os
import sys
from importlib import metadata
from pathlib import Path

PROFILE = "agx-cu118-ort1201-py312"
LOCK = Path(__file__).resolve().parents[1] / "requirements-jetson-ai.lock"


def gpu_report():
    driver = ctypes.CDLL("libcuda.so.1")
    status = driver.cuInit(0)
    if status:
        raise RuntimeError(f"CUDA driver initialization failed: {status}; check CUDA 11.8 compat libraries")
    count, version = ctypes.c_int(), ctypes.c_int()
    if driver.cuDeviceGetCount(ctypes.byref(count)) or count.value < 1:
        raise RuntimeError("CUDA driver sees no GPU")
    if driver.cuDriverGetVersion(ctypes.byref(version)) or version.value < 11080:
        raise RuntimeError("CUDA driver must support at least 11.8")
    major, minor = ctypes.c_int(), ctypes.c_int()
    if driver.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), 0):
        raise RuntimeError("Could not query GPU compute capability")
    if (major.value, minor.value) != (7, 2):
        raise RuntimeError("This profile expects Xavier compute capability 7.2")
    runtime = ctypes.CDLL("libcudart.so.11.0")
    runtime_version = ctypes.c_int()
    if runtime.cudaRuntimeGetVersion(ctypes.byref(runtime_version)) or runtime_version.value != 11080:
        raise RuntimeError("Expected CUDA runtime 11.8")
    cudnn = ctypes.CDLL("libcudnn.so.8")
    cudnn.cudnnGetVersion.restype = ctypes.c_size_t
    cudnn_version = cudnn.cudnnGetVersion()
    if cudnn_version != 8600:
        raise RuntimeError(f"Expected cuDNN 8.6.0, got {cudnn_version}")
    return {"cuda_driver": version.value, "cuda_runtime": runtime_version.value,
            "cudnn": cudnn_version, "compute_capability": "7.2"}


def runtime_report():
    import onnxruntime as ort
    from packaging.requirements import Requirement

    if sys.version_info[:3] != (3, 12, 15):
        raise RuntimeError("Expected private CPython 3.12.15")
    if ort.__version__ != "1.20.1" or "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("Expected ONNX Runtime 1.20.1 GPU")
    platform_fix = Path(sys.prefix) / "ort-platform-fix"
    if not platform_fix.is_file() or platform_fix.read_text().strip() != "ort-arm-cpuid-bounds-v1":
        raise RuntimeError("ORT wheel needs upstream ARM CPUID bounds fix; rerun updated setup")
    try:
        metadata.version("onnxruntime")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError("CPU onnxruntime distribution overlaps onnxruntime-gpu; recreate the private venv")
    if metadata.version("numpy") != "1.26.4" or metadata.version("zerotts") != "0.1.5":
        raise RuntimeError("Expected NumPy 1.26.4 and ZeroTTS 0.1.5")
    for text in metadata.requires("zerotts") or []:
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        # Both ORT distributions provide the same import; never install both.
        name = "onnxruntime-gpu" if requirement.name == "onnxruntime" else requirement.name
        version = metadata.version(name)
        if requirement.specifier and version not in requirement.specifier:
            raise RuntimeError(f"Dependency mismatch: {requirement}, installed {version}")
    return {"profile": PROFILE, "python": sys.version.split()[0],
            "ort_platform_fix": platform_fix.read_text().strip(),
            "onnxruntime": ort.__version__, "numpy": metadata.version("numpy"),
            "zerotts": metadata.version("zerotts"),
            "lock_sha256": hashlib.sha256(
                LOCK.read_bytes() + LOCK.with_name("requirements-jetson-zerotts.lock").read_bytes()
            ).hexdigest(), **gpu_report()}


def artifact_report():
    from prepare_speech_models import TTS_FRAME_SHA256, sha256

    data = Path(os.environ.get("JETSON_AI_DATA_DIR", "/mnt/jetson-data/vivi-ai"))
    decoder = data / "models/speech/zerotts/onnx/local_frame_decode.onnx"
    if sha256(decoder) != TTS_FRAME_SHA256:
        raise RuntimeError("ZeroTTS decoder differs from pinned original model; rerun setup")
    return {"tts_decoder_sha256": TTS_FRAME_SHA256,
            "ort_binary_sha256": sha256(Path(sys.prefix) / "lib/python3.12/site-packages/onnxruntime/capi/onnxruntime_pybind11_state.so"),
            "llama_sha256": sha256(data / "build/llama.cpp/build-cu118-gcc10/bin/llama-server"),
            "whisper_sha256": sha256(data / "build/whisper.cpp/build-cu118-gcc10/bin/whisper-server")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-only", action="store_true")
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--check-manifest", type=Path)
    args = parser.parse_args()
    report = gpu_report() if args.gpu_only else runtime_report()
    if args.check_manifest or args.write_manifest:
        report.update(artifact_report())
    if args.check_manifest:
        saved = json.loads(args.check_manifest.read_text())
        # Driver micro updates may report a different driver API level. Compare
        # the profile and actual library versions; recheck GPU access every run.
        for key in ("profile", "python", "onnxruntime", "numpy", "zerotts", "ort_platform_fix", "ort_binary_sha256",
                    "lock_sha256", "cuda_runtime", "cudnn", "compute_capability",
                    "tts_decoder_sha256", "llama_sha256", "whisper_sha256"):
            if saved.get(key) != report[key]:
                raise RuntimeError(f"Setup manifest differs at {key}; rerun setup")
    if args.write_manifest:
        temporary = args.write_manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(args.write_manifest)
    print(json.dumps(report, indent=2))
