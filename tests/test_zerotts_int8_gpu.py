import json
import shutil
import subprocess

import numpy as np
import pytest

from jetson_ai.tensorrt_tts import int8_compute_layers, profile_counts, require_trt_execution
from jetson_ai.tts_profile import resolve_tts_model
from scripts import prepare_ort_trt_inspection, prepare_speech_models
from scripts.quantize_zerotts_gpu import FeedRecorder, quantize_graph


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
    events = [{"cat": "Node", "args": {"provider": "CUDAExecutionProvider"}},
              {"cat": "Session", "args": {"provider": "TensorrtExecutionProvider"}}]
    counts = profile_counts(events)
    assert counts == {"CUDAExecutionProvider": 1}
    with pytest.raises(RuntimeError, match="no TensorRT execution"):
        require_trt_execution({name: counts for name in ("text_encoder", "prefix_step", "local_frame_decode")})


def test_reformat_int8_is_not_compute_precision_evidence():
    layers = [{"Name": "reformat", "LayerType": "Reformat", "Format": "Int8"},
              {"Name": "float-gemm", "LayerType": "CaskGemm", "Format": "Float"},
              {"Name": "int8-in-name-only", "LayerType": "CaskGemm", "Format": "Float"},
              {"Name": "int8-gemm", "LayerType": "CaskGemm", "Inputs": [{"Format/Datatype": "Int8"}]}]
    assert int8_compute_layers(layers) == ["int8-gemm"]


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
    original = (prepare_ort_trt_inspection.ORIGINAL + "\n" + prepare_ort_trt_inspection.HARDWARE_ORIGINAL + "\n") * 2
    path.write_text(original)
    prepare_ort_trt_inspection.prepare(path)
    assert path.read_text().count("setProfilingVerbosity") == 2
    assert path.read_text().count(prepare_ort_trt_inspection.HARDWARE_FIXED) == 2
    prepare_ort_trt_inspection.prepare(path)
    assert path.read_text().count("setProfilingVerbosity") == 2
    assert path.with_name(path.name + ".before-trt-inspection").read_text() == original
    path.write_text("unexpected")
    with pytest.raises(ValueError, match="Unexpected"):
        prepare_ort_trt_inspection.prepare(path)


def test_trt_patch_upgrades_existing_inspection_source(tmp_path):
    path = tmp_path / "provider.cc"
    path.write_text((prepare_ort_trt_inspection.FIXED + "\n" + prepare_ort_trt_inspection.HARDWARE_ORIGINAL + "\n") * 2)
    prepare_ort_trt_inspection.prepare(path)
    assert path.read_text().count("setProfilingVerbosity") == 2
    assert path.read_text().count(prepare_ort_trt_inspection.HARDWARE_FIXED) == 2


@pytest.mark.parametrize("minor", [5, 6])
def test_hardware_compatibility_guard_compiles_with_old_and_new_trt_api(tmp_path, minor):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    path = tmp_path / "guard.cc"
    # TRT 8.5 deliberately has neither the enum nor the builder method.
    api = """namespace nvinfer1 { enum class HardwareCompatibilityLevel { kAMPERE_PLUS }; }
struct Config { void setHardwareCompatibilityLevel(nvinfer1::HardwareCompatibilityLevel) {} };""" if minor >= 6 else "struct Config {};"
    path.write_text(f"#define NV_TENSORRT_MAJOR 8\n#define NV_TENSORRT_MINOR {minor}\n"
                    "#define ORT_MAKE_STATUS(...) -1\n" + api + "\nint build(Config* trt_config) {\n"
                    + prepare_ort_trt_inspection.HARDWARE_FIXED + "\nreturn 0;\n}\n")
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
    graph = helper.make_graph([
        helper.make_node("Identity", ["weight"], ["alias"]),
        helper.make_node("MatMul", ["x", "alias"], ["y"]),
        helper.make_node("Identity", ["ids"], ["ids_out"]),
    ], "model", [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4]),
                  helper.make_tensor_value_info("ids", TensorProto.INT64, [1])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 4]),
         helper.make_tensor_value_info("ids_out", TensorProto.INT64, [1])],
        initializer=[numpy_helper.from_array(weights, "weight")])
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
