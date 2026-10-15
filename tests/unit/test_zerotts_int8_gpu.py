import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from jetson_ai.tensorrt_tts import int8_compute_layers, profile_counts, require_trt_execution
from jetson_ai.tts_profile import resolve_tts_model
from scripts import prepare_ort_trt_inspection, prepare_speech_models
from scripts.quantize_zerotts_gpu import FeedRecorder, quantize_graph


def test_tensor_rt_failure_does_not_reset_session_to_cuda(tmp_path, monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    from onnxruntime.capi import _pybind_state

    from jetson_ai import tensorrt_tts

    path = tmp_path / "model"
    path.mkdir()
    (path / "quantization.json").write_text('{"output_sha256":{"graph":"hash"}}')
    monkeypatch.setattr(tensorrt_tts, "device_identity", lambda: {"gpu": "test"})
    monkeypatch.setattr(tensorrt_tts, "prepare_linear_graph", lambda source, destination: source)
    monkeypatch.setattr(
        ort,
        "get_available_providers",
        lambda: ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    original = ort.InferenceSession
    provider_configs = []

    class NativeSession:
        def run(self, *args):
            raise _pybind_state.EPFail("TensorRT engine build failed")

    def create_session(path, *, sess_options, providers):
        assert os.environ["VIVI_TRT_INT8_LINEAR_ONLY"] == "1"
        provider_configs.append(providers)
        # Exercise ORT's real run()/disable_fallback() logic with a failing
        # native backend, so an accidental CUDA retry fails this regression.
        session = object.__new__(original)
        session._sess = NativeSession()
        session._enable_fallback = True
        session._inputs_meta = []
        session._outputs_meta = [SimpleNamespace(name="y")]
        session._providers = [p[0] if isinstance(p, tuple) else p for p in providers]
        session._fallback_providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        def reset(*args):
            pytest.fail("TensorRT failure must not recreate a CUDA session")

        session.set_providers = reset
        return session

    monkeypatch.setattr(ort, "InferenceSession", create_session)
    model = SimpleNamespace()
    sessions, _, cache = tensorrt_tts.configure_sessions(model, path)
    for session in sessions.values():
        with pytest.raises(_pybind_state.EPFail, match="engine build failed"):
            session.run(None, {})
    assert all(p[1][1]["arena_extend_strategy"] == "kSameAsRequested" for p in provider_configs)
    assert all(p[1][1]["cudnn_conv_algo_search"] == "HEURISTIC" for p in provider_configs)
    assert all(p[1][1]["cudnn_conv_use_max_workspace"] is False for p in provider_configs)
    assert all(p[0][1]["trt_min_subgraph_size"] == 1 for p in provider_configs)
    assert all(p[0][1]["trt_context_memory_sharing_enable"] is True for p in provider_configs)
    assert "VIVI_TRT_INT8_LINEAR_ONLY" not in os.environ
    # Different allocation settings must not reuse a previous engine cache.
    monkeypatch.setattr(tensorrt_tts, "CUDA_OPTIONS", {"arena_extend_strategy": "kNextPowerOfTwo"})
    _, _, other_cache = tensorrt_tts.configure_sessions(model, path)
    assert other_cache != cache


def test_wav_uses_bounded_streaming_codec_and_closes_generator():
    import io
    import wave

    from jetson_ai.speech import SpeechModels

    calls, closed = [], []
    voice = object()

    class Model:
        sample_rate = 48000

        def synthesize(self, *args, **kwargs):
            pytest.fail("WAV must not decode the full utterance at once")

        def synthesize_stream(self, text, **kwargs):
            calls.append((text, kwargs))
            try:
                yield np.array([[0, 0.5]], dtype=np.float32)
                yield np.array([[1, -1]], dtype=np.float32)
            finally:
                closed.append(True)

    models = SpeechModels()
    models.models["tts"] = Model(), voice
    data = models.synthesize("Xin chào")
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()) == (48000, 1, 2, 4)
        np.testing.assert_array_equal(np.frombuffer(wav.readframes(4), dtype="<i2"), [0, 16383, 32767, -32767])
    assert calls == [("Xin chào", {"voice": voice, "max_chunk_frames": 4})]
    assert closed == [True]


def test_streaming_codec_rejects_nonfinite_audio_and_closes_generator():
    from jetson_ai.speech import SpeechModels

    closed = []

    class Model:
        sample_rate = 48000

        def synthesize_stream(self, *args, **kwargs):
            try:
                yield np.array([np.nan], dtype=np.float32)
            finally:
                closed.append(True)

    models = SpeechModels()
    models.models["tts"] = Model(), None
    with pytest.raises(RuntimeError, match="non-finite"):
        models.synthesize("Xin chào")
    assert closed == [True]


@pytest.mark.parametrize("failed_verification", [False, True])
def test_gpu_codec_is_promoted_only_after_tensor_rt_verification(tmp_path, monkeypatch, failed_verification):
    import io
    import zipfile

    ort = pytest.importorskip("onnxruntime")
    zerotts = pytest.importorskip("zerotts")
    from jetson_ai import tensorrt_tts
    from jetson_ai.speech import SpeechModels

    base = tmp_path / "zerotts"
    base.mkdir()
    gpu_model = tmp_path / "zerotts-int8-gpu"
    gpu_model.mkdir()
    (gpu_model / "quantization.json").write_text(
        json.dumps({"format": "int8-qdq-mixed", "execution_provider": "TensorrtExecutionProvider"})
    )
    voice = tmp_path / "voice.zip"
    data = io.BytesIO()
    np.savez(data, voice_emb=np.zeros((1, 1, 2), dtype=np.float32))
    with zipfile.ZipFile(voice, "w") as archive:
        archive.writestr("VIVI/meta.json", "{}")
        archive.writestr("VIVI/voice.npz", data.getvalue())
    monkeypatch.setenv("JETSON_AI_TTS_MODEL", str(base))
    monkeypatch.setenv("JETSON_AI_TTS_PRECISION", "int8-gpu")
    monkeypatch.setenv("JETSON_AI_DEVICE", "cuda")
    monkeypatch.setenv("JETSON_AI_VOICE_PACK", str(voice))
    monkeypatch.setattr(
        ort,
        "get_available_providers",
        lambda: ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setattr(ort, "preload_dlls", lambda **kwargs: None, raising=False)
    events = []

    class Session:
        def __init__(self, provider):
            self.provider = provider

        def get_providers(self):
            return [self.provider]

        def set_providers(self, providers):
            events.append("codec_cuda")
            self.provider = providers[0][0]

        def disable_fallback(self):
            pass

    codec = SimpleNamespace(
        _decode_full_sess=Session("CPUExecutionProvider"), _decode_step_sess=Session("CPUExecutionProvider")
    )
    model = SimpleNamespace(codec=codec, n_voice_queries=1, d_model=2, sample_rate=48000)

    def bootstrap(*args, **kwargs):
        assert kwargs["providers"] == ["CPUExecutionProvider"]
        assert kwargs["warmup"] is False
        events.append("bootstrap_cpu")
        return model

    def configure(model, path):
        sessions = {}
        for name, attribute in tensorrt_tts.ATTRIBUTES.items():
            session = Session("TensorrtExecutionProvider")
            setattr(model, attribute, session)
            sessions[name] = session
        return sessions, {"gpu": "test"}, tmp_path

    def verify(model, voice, sessions, cache):
        assert codec._decode_full_sess.provider == codec._decode_step_sess.provider == "CPUExecutionProvider"
        events.append("verify_trt")
        if failed_verification:
            raise RuntimeError("TensorRT OOM")
        return {"providers": {}, "int8_compute_layers": {"text_encoder": ["gemm"]}}

    monkeypatch.setattr(zerotts.ZeroTTS, "from_pretrained", bootstrap)
    monkeypatch.setattr(tensorrt_tts, "configure_sessions", configure)
    monkeypatch.setattr(tensorrt_tts, "verify_execution", verify)
    models = SpeechModels()
    models.load("tts")
    if failed_verification:
        assert events == ["bootstrap_cpu", "verify_trt"]
        assert models.info["tts"]["status"] == "unavailable"
        assert models.info["tts"]["using_gpu"] is False
        assert "TensorRT OOM" in models.info["tts"]["error"]
    else:
        assert events == ["bootstrap_cpu", "verify_trt", "codec_cuda", "codec_cuda"]
        assert models.info["tts"]["status"] == "ready"
        assert models.info["tts"]["using_gpu"] is True


def test_gpu_artifact_is_separate_and_cannot_accept_cpu_quantization(tmp_path):
    model = tmp_path / "zerotts-int8-gpu"
    model.mkdir()
    manifest = model / "quantization.json"
    manifest.write_text(json.dumps({"format": "int8-qdq-mixed", "execution_provider": "TensorrtExecutionProvider"}))
    assert resolve_tts_model(tmp_path / "zerotts", "int8-gpu") == (model, "int8-qdq-mixed")
    assert resolve_tts_model(model) == (model, "int8-qdq-mixed")
    manifest.write_text(json.dumps({"format": "int8-dynamic-mixed", "execution_provider": "CPUExecutionProvider"}))
    with pytest.raises(ValueError, match="Unsupported"):
        resolve_tts_model(model)


def test_provider_registration_is_not_execution_evidence():
    events = [
        {"cat": "Node", "args": {"provider": "CUDAExecutionProvider"}},
        {"cat": "Session", "args": {"provider": "TensorrtExecutionProvider"}},
    ]
    counts = profile_counts(events)
    assert counts == {"CUDAExecutionProvider": 1}
    with pytest.raises(RuntimeError, match="no TensorRT execution"):
        require_trt_execution({name: counts for name in ("text_encoder", "prefix_step", "local_frame_decode")})


def test_reformat_int8_is_not_compute_precision_evidence():
    layers = [
        {"Name": "reformat", "LayerType": "Reformat", "Format": "Int8"},
        {"Name": "float-gemm", "LayerType": "CaskGemm", "Format": "Float"},
        {"Name": "int8-in-name-only", "LayerType": "CaskGemm", "Format": "Float"},
        {"Name": "int8-gemm", "LayerType": "CaskGemm", "Inputs": [{"Format/Datatype": "Int8"}]},
    ]
    assert int8_compute_layers(layers) == ["int8-gemm"]


def test_dynamic_engine_int8_compute_tactic_is_precision_evidence():
    layers = [
        {
            "Name": "linear",
            "LayerType": "CaskConvolution",
            "Inputs": [{"Format/Datatype": "N/A due to dynamic shapes"}],
            "TacticName": "volta_fp32_i8816cudnn_int8_128x128_ldg16_relu_small_nt_v1",
        },
        {"Name": "cast", "LayerType": "Reformat", "TacticName": "int8_to_fp32"},
    ]
    assert int8_compute_layers(layers) == ["linear"]
    layers[0]["TacticName"] = "volta_fp32_convolution"
    assert int8_compute_layers(layers) == []


def test_feed_recording_is_bounded_and_copies_mutable_inputs():
    class Session:
        def run(self, outputs, feeds):
            return [feeds["x"]]

    recorder = FeedRecorder(Session(), limit=3)
    value = np.ones((1, 2), dtype=np.float32)
    for index in range(20):
        value.fill(index)
        recorder.run(None, {"x": value})
    assert len(recorder.samples) == 3
    assert recorder.seen == 20
    np.testing.assert_array_equal(recorder.samples[0]["x"], np.zeros((1, 2)))


def test_trt_inspection_patch_is_guarded_and_idempotent(tmp_path):
    path = tmp_path / "provider.cc"
    original = (
        (prepare_ort_trt_inspection.ORIGINAL + "\n" + prepare_ort_trt_inspection.HARDWARE_ORIGINAL + "\n") * 2
        + prepare_ort_trt_inspection.CAPABILITY_ORIGINAL
        + (prepare_ort_trt_inspection.CONTEXT_ORIGINAL + "\n" + prepare_ort_trt_inspection.BUFFER_ORIGINAL + "\n") * 2
    )
    path.write_text(original)
    prepare_ort_trt_inspection.prepare(path)
    assert path.read_text().count("setProfilingVerbosity") == 2
    assert path.read_text().count("setPreviewFeature") == 2
    assert path.read_text().count(prepare_ort_trt_inspection.HARDWARE_FIXED) == 2
    assert path.read_text().count("VIVI_TRT_INT8_LINEAR_ONLY") == 1
    assert path.read_text().count(prepare_ort_trt_inspection.BUFFER_FIXED) == 2
    prepare_ort_trt_inspection.prepare(path)
    assert path.read_text().count("setProfilingVerbosity") == 2
    assert path.read_text().count("setPreviewFeature") == 2
    assert path.with_name(path.name + ".before-trt-inspection").read_text() == original
    path.write_text("unexpected")
    with pytest.raises(ValueError, match="Unexpected"):
        prepare_ort_trt_inspection.prepare(path)


def test_trt_patch_upgrades_existing_inspection_source(tmp_path):
    path = tmp_path / "provider.cc"
    path.write_text(
        (prepare_ort_trt_inspection.FIXED + "\n" + prepare_ort_trt_inspection.HARDWARE_ORIGINAL + "\n") * 2
        + prepare_ort_trt_inspection.CAPABILITY_ORIGINAL
        + (prepare_ort_trt_inspection.CONTEXT_ORIGINAL + "\n" + prepare_ort_trt_inspection.BUFFER_ORIGINAL + "\n") * 2
    )
    prepare_ort_trt_inspection.prepare(path)
    assert path.read_text().count("setProfilingVerbosity") == 2
    assert path.read_text().count("setPreviewFeature") == 2
    assert path.read_text().count(prepare_ort_trt_inspection.HARDWARE_FIXED) == 2


def test_shared_context_buffer_lives_through_enqueue(tmp_path):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    source = tmp_path / "lifetime.cc"
    source.write_text(
        """
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <memory>
bool alive = false;
struct IAllocator {
  template<class T> static auto MakeUniquePtrFromOrtAllocator(void*, size_t size) {
    alive = true;
    return std::unique_ptr<void, std::function<void(void*)>>(std::calloc(size, 1), [](void* p) {
      std::free(p); alive = false;
    });
  }
};
struct State { bool context_memory_sharing_enable = true; };
struct Context {
  void* buffer = nullptr;
  void setDeviceMemory(void* p) {
    assert(reinterpret_cast<uintptr_t>(p) % 512 == 0);
    buffer = p; *static_cast<int*>(buffer) = 42;
  }
  void enqueue() { assert(alive); assert(*static_cast<int*>(buffer) == 42); }
};
int main() {
  State state; auto* trt_state = &state;
  Context context; auto* trt_context = &context;
  void* alloc = nullptr; size_t size = 16; auto* max_context_mem_size_ptr = &size;
  {
"""
        + prepare_ort_trt_inspection.CONTEXT_FIXED
        + "\n"
        + prepare_ort_trt_inspection.BUFFER_FIXED
        + "\n}\ntrt_context->enqueue();\n}\nassert(!alive);\n}\n"
    )
    executable = tmp_path / "lifetime"
    subprocess.run([compiler, "-std=c++17", str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)


@pytest.mark.parametrize("minor", [5, 6])
def test_dynamic_shapes_preview_guard_compiles_for_jetpack_api(tmp_path, minor):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    preview = prepare_ort_trt_inspection.DYNAMIC_FIXED.removeprefix(prepare_ort_trt_inspection.FIXED)
    api = (
        "namespace nvinfer1 { enum class PreviewFeature { kFASTER_DYNAMIC_SHAPES_0805 }; }\n"
        "struct Config { void setPreviewFeature(nvinfer1::PreviewFeature, bool) {} };"
        if minor == 5
        else "struct Config {};"
    )
    path = tmp_path / "preview.cc"
    path.write_text(
        f"#define NV_TENSORRT_MAJOR 8\n#define NV_TENSORRT_MINOR {minor}\n"
        + api
        + "\nvoid configure(Config* trt_config) {\n"
        + preview
        + "\n}\n"
    )
    subprocess.run([compiler, "-std=c++17", "-fsyntax-only", str(path)], check=True, capture_output=True)


@pytest.mark.parametrize("minor", [5, 6])
def test_hardware_compatibility_guard_compiles_with_old_and_new_trt_api(tmp_path, minor):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    path = tmp_path / "guard.cc"
    # TRT 8.5 deliberately has neither the enum nor the builder method.
    api = (
        """namespace nvinfer1 { enum class HardwareCompatibilityLevel { kAMPERE_PLUS }; }
struct Config { void setHardwareCompatibilityLevel(nvinfer1::HardwareCompatibilityLevel) {} };"""
        if minor >= 6
        else "struct Config {};"
    )
    path.write_text(
        f"#define NV_TENSORRT_MAJOR 8\n#define NV_TENSORRT_MINOR {minor}\n"
        "#define ORT_MAKE_STATUS(...) -1\n"
        + api
        + "\nint build(Config* trt_config) {\n"
        + prepare_ort_trt_inspection.HARDWARE_FIXED
        + "\nreturn 0;\n}\n"
    )
    subprocess.run([compiler, "-std=c++17", "-fsyntax-only", str(path)], check=True, capture_output=True)


def test_gpu_preparation_can_defer_active_profile(tmp_path, monkeypatch):
    from scripts import quantize_zerotts_gpu

    (tmp_path / "whisper").mkdir()
    source = tmp_path / "zerotts/onnx/local_frame_decode.onnx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    monkeypatch.setattr(prepare_speech_models, "TTS_FRAME_SHA256", prepare_speech_models.sha256(source))
    profile = tmp_path / "tts-profile.json"
    profile.write_text('{"precision":"fp32"}')
    monkeypatch.setattr(quantize_zerotts_gpu, "quantize_zerotts_gpu", lambda *a, **kw: None)
    prepare_speech_models.prepare(tmp_path, "transformers", "int8-gpu", local_only=True, activate=False)
    assert json.loads(profile.read_text()) == {"precision": "fp32"}


def test_static_qdq_inference_preserves_integer_io_and_float_outputs(tmp_path):
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper

    source = tmp_path / "model.onnx"
    output = tmp_path / "int8.onnx"
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    weights = np.linspace(-1, 1, 16, dtype=np.float32).reshape(4, 4)
    graph = helper.make_graph(
        [
            helper.make_node("Identity", ["weight"], ["alias"]),
            helper.make_node("MatMul", ["x", "alias"], ["y"]),
            helper.make_node("Identity", ["ids"], ["ids_out"]),
        ],
        "model",
        [
            helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4]),
            helper.make_tensor_value_info("ids", TensorProto.INT64, [1]),
        ],
        [
            helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 4]),
            helper.make_tensor_value_info("ids_out", TensorProto.INT64, [1]),
        ],
        initializer=[numpy_helper.from_array(weights, "weight")],
    )
    onnx.save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9), str(source))
    x = np.array([[0.25, -0.5, 0.75, 1]], dtype=np.float32)
    for index in range(3):
        np.savez(feeds / f"{index}.npz", x=x, ids=np.array([1000], dtype=np.int64))
    report = quantize_graph(source, output, feeds, tmp_path)
    assert report["int8_qdq_matmul_nodes"] == 1
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    session = ort.InferenceSession(str(output), sess_options=options, providers=["CPUExecutionProvider"])
    actual, ids = session.run(None, {"x": x, "ids": np.array([1000], dtype=np.int64)})
    np.testing.assert_allclose(actual, x @ weights, atol=0.03)
    np.testing.assert_array_equal(ids, [1000])
    assert ids.dtype == np.int64
