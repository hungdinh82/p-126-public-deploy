"""TensorRT QDQ sessions with a real-inference check and per-device caches."""

import ctypes
import hashlib
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

from jetson_ai.trt_linear import LINEAR_RECIPE, prepare_linear_graph

ATTRIBUTES = {"text_encoder": "text_encoder_sess", "prefix_step": "prefix_step_sess",
              "local_frame_decode": "local_frame_decode_sess"}
CUDA_OPTIONS = {"arena_extend_strategy": "kSameAsRequested", "cudnn_conv_algo_search": "HEURISTIC",
                "cudnn_conv_use_max_workspace": False}
MIN_SUBGRAPH_SIZE = 1
CODEC_CHUNK_FRAMES = 4


def device_identity() -> dict:
    import onnxruntime as ort

    from scripts.quantize_zerotts import file_hash

    provider = Path(ort.__file__).parent / "capi/libonnxruntime_providers_tensorrt.so"
    if not provider.is_file() or b"VIVI_TRT_INT8_LINEAR_ONLY" not in provider.read_bytes():
        raise RuntimeError("Installed TensorRT provider lacks INT8 linear support; rerun Xavier int8-gpu setup")
    # The Xavier deployment deliberately uses JetPack's TensorRT 8 libraries.
    # Loading them here catches missing dependencies before ORT silently falls back.
    ctypes.CDLL("libnvinfer.so.8")
    ctypes.CDLL("libnvonnxparser.so.8")
    driver = ctypes.CDLL("libcuda.so.1")
    if driver.cuInit(0):
        raise RuntimeError("TensorRT requires an accessible CUDA GPU")
    name = ctypes.create_string_buffer(256)
    uuid = ctypes.create_string_buffer(16)
    major, minor = ctypes.c_int(), ctypes.c_int()
    version = ctypes.c_int()
    if (driver.cuDeviceGetName(name, len(name), 0)
            or driver.cuDeviceGetUuid(uuid, 0)
            or driver.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), 0)
            or driver.cuDriverGetVersion(ctypes.byref(version))):
        raise RuntimeError("Cannot identify GPU for TensorRT engine cache")
    libraries = {}
    for soname in ("libnvinfer.so.8", "libnvonnxparser.so.8"):
        matches = [p for base in (Path("/usr/lib/aarch64-linux-gnu"), Path("/usr/lib/x86_64-linux-gnu"))
                   if (p := base / soname).is_file()]
        if not matches:
            raise RuntimeError(f"Cannot fingerprint JetPack library {soname}")
        # Engine compatibility depends on the exact native TensorRT build.
        libraries[soname] = file_hash(matches[0])
    libraries["onnxruntime_tensorrt_provider"] = file_hash(provider)
    return {"gpu": name.value.decode(), "uuid": uuid.raw.hex(), "compute_capability": f"{major.value}.{minor.value}",
            "driver": version.value, "libraries": libraries}


def profile_counts(events: list[dict]) -> dict[str, int]:
    return dict(Counter(event.get("args", {}).get("provider") for event in events
                        if event.get("cat") == "Node" and event.get("args", {}).get("provider")))


def require_trt_execution(reports: dict) -> None:
    for name in ATTRIBUTES:
        if reports.get(name, {}).get("TensorrtExecutionProvider", 0) < 1:
            raise RuntimeError(f"{name}: no TensorRT execution observed; GPU INT8 profile rejected")


def int8_compute_layers(layers: list[dict]) -> list[str]:
    def has_int8_format(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in ("format/datatype", "datatype", "dtype", "precision", "format"):
                    if isinstance(item, str) and "int8" in item.lower():
                        return True
                if has_int8_format(item):
                    return True
        elif isinstance(value, list):
            return any(has_int8_format(item) for item in value)
        return False

    result = []
    for layer in layers:
        kind = str(layer.get("LayerType", layer.get("ParameterType", ""))).lower()
        if not any(word in kind for word in ("matrix", "matmul", "gemm", "fullyconnected", "convolution")):
            continue
        # TRT 8.5 can report N/A formats for dynamic engines even with an
        # inspection context. Selected compute tactics identify INT8 kernels.
        tactic = str(layer.get("TacticName", "")).lower()
        if has_int8_format(layer) or "int8" in tactic:
            result.append(str(layer.get("Name", kind)))
    return result


def inspect_int8_engines(cache: Path) -> dict:
    inspector = Path(os.environ.get("JETSON_AI_TRT_INSPECTOR", ""))
    if not inspector.is_file():
        raise FileNotFoundError("TensorRT engine inspector missing; rerun Xavier setup --tts-precision int8-gpu")
    reports = {}
    for name in ATTRIBUTES:
        layers = []
        folder = cache / (name + "-" + LINEAR_RECIPE)
        engines = sorted(folder.glob("*.engine"))
        if engines:
            reply = subprocess.run([str(inspector.resolve()), *(str(engine) for engine in engines)],
                                   check=True, capture_output=True, text=True, timeout=120)
            info = json.loads(reply.stdout)
            # One runtime/context per graph avoids hundreds of CUDA startups.
            batch = [info] if isinstance(info, dict) else info
            for item in batch:
                layers.extend(int8_compute_layers(item.get("Layers", [])))
        if not layers:
            raise RuntimeError(f"{name}: engine inspection found no INT8 compute layers; profile rejected")
        reports[name] = layers
    return reports


def configure_sessions(model, path: Path):
    import onnxruntime as ort

    if "TensorrtExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("ORT lacks TensorRT; rerun Xavier setup --tts-precision int8-gpu")
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("CUDAExecutionProvider is required for TensorRT graph fallback")
    identity = device_identity()
    artifact = json.loads((path / "quantization.json").read_text())
    key = hashlib.sha256(json.dumps({"device": identity, "ort": ort.__version__,
                                   "artifact": artifact["output_sha256"], "workspace": 512 * 1024**2,
                                   "fp16": False, "inspection_recipe": 1,
                                   "min_subgraph_size": MIN_SUBGRAPH_SIZE,
                                   "context_memory_sharing": True,
                                   "cuda_options": CUDA_OPTIONS}, sort_keys=True).encode()).hexdigest()[:24]
    cache = path.parent / "trt-cache" / key
    for attribute in ATTRIBUTES.values():
        setattr(model, attribute, None)
    sessions = {}
    for name, attribute in ATTRIBUTES.items():
        folder = cache / (name + "-" + LINEAR_RECIPE)
        folder.mkdir(parents=True, exist_ok=True)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        # Keep calibrated Q/DQ visible to TensorRT; CPU-oriented ORT fusions
        # can otherwise replace them with operators TensorRT cannot consume.
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        options.enable_profiling = True
        options.profile_file_prefix = str(folder / "execution")
        # QDQ explicitly carries calibrated scales, so no calibration table is
        # supplied. CUDA/CPU handle unsupported shape/sampling operators.
        providers = [("TensorrtExecutionProvider", {
            "trt_int8_enable": True, "trt_fp16_enable": False,
            "trt_context_memory_sharing_enable": True,
            # The native opt-in policy groups only Conv with its QDQ inputs.
            # Attention, KV cache and sampling stay on CUDA/CPU.
            "trt_min_subgraph_size": MIN_SUBGRAPH_SIZE,
            "trt_max_workspace_size": 512 * 1024**2,
            "trt_engine_cache_enable": True, "trt_engine_cache_path": str(folder),
            "trt_timing_cache_enable": True, "trt_timing_cache_path": str(folder),
        }), ("CUDAExecutionProvider", dict(CUDA_OPTIONS)), "CPUExecutionProvider"]
        graph_path = prepare_linear_graph(path / "onnx" / f"{name}.onnx", folder / f"{name}.onnx")
        # Only native graph partitioning reads this opt-in variable. Restore
        # the caller's environment after session creation.
        previous = os.environ.get("VIVI_TRT_INT8_LINEAR_ONLY")
        os.environ["VIVI_TRT_INT8_LINEAR_ONLY"] = "1"
        try:
            session = ort.InferenceSession(str(graph_path), sess_options=options, providers=providers)
        finally:
            if previous is None:
                os.environ.pop("VIVI_TRT_INT8_LINEAR_ONLY", None)
            else:
                os.environ["VIVI_TRT_INT8_LINEAR_ONLY"] = previous
        session.disable_fallback()
        if session.get_providers()[0] != "TensorrtExecutionProvider":
            raise RuntimeError(f"{name}: TensorRT failed to initialize; refusing CUDA/CPU replacement")
        setattr(model, attribute, session)
        sessions[name] = session
    return sessions, identity, cache


def verify_execution(model, voice, sessions: dict, cache: Path) -> dict:
    import numpy as np

    # Running the actual generation loop is necessary: provider registration
    # alone says nothing about where these graphs execute.
    rng_state = np.random.get_state()
    try:
        np.random.seed(42)
        stream = model.synthesize_stream("Xin chào bạn.", voice=voice, max_frames=12,
                                         max_chunk_frames=CODEC_CHUNK_FRAMES)
        try:
            chunks = [np.asarray(chunk).reshape(-1) for chunk in stream]
        finally:
            stream.close()
        audio = np.concatenate(chunks) if chunks else np.empty(0)
        if not audio.size or not np.isfinite(audio).all() or not np.any(audio):
            raise RuntimeError("TensorRT warmup produced empty/non-finite/silent audio")
    finally:
        np.random.set_state(rng_state)
        reports = {}
        for name, session in sessions.items():
            report = Path(session.end_profiling())
            reports[name] = profile_counts(json.loads(report.read_text()))
    require_trt_execution(reports)
    # Require INT8 compute layers, not just a registered provider or Q/DQ casts.
    layers = inspect_int8_engines(cache)
    verification = {"providers": reports, "int8_compute_layers": layers,
                    "internal_layer_precision_verified": True}
    (cache / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    return verification
