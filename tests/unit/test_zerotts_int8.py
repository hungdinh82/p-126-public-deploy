import json

import numpy as np
import pytest

from jetson_ai.tts_profile import resolve_tts_model
from scripts import prepare_speech_models, quantize_zerotts


def test_legacy_model_and_explicit_fp32_override(tmp_path):
    base = tmp_path / "zerotts"
    assert resolve_tts_model(base) == (base, "fp32")
    (tmp_path / "tts-profile.json").write_text('{"precision":"int8"}')
    assert resolve_tts_model(base, "fp32") == (base, "fp32")
    with pytest.raises(FileNotFoundError, match="INT8 model missing"):
        resolve_tts_model(base)


def test_selected_int8_requires_supported_manifest(tmp_path):
    output = tmp_path / "zerotts-int8"
    output.mkdir()
    manifest = output / "quantization.json"
    manifest.write_text('{"format":"int8-dynamic-mixed","execution_provider":"CPUExecutionProvider"}')
    (tmp_path / "tts-profile.json").write_text('{"precision":"int8"}')
    assert resolve_tts_model(tmp_path / "zerotts") == (output, "int8-dynamic-mixed")
    assert resolve_tts_model(output) == (output, "int8-dynamic-mixed")
    manifest.write_text('{"format":"unknown"}')
    with pytest.raises(ValueError, match="Unsupported"):
        resolve_tts_model(output)


def test_failed_conversion_preserves_active_profile(tmp_path, monkeypatch):
    (tmp_path / "whisper").mkdir()
    graph = tmp_path / "zerotts/onnx/local_frame_decode.onnx"
    graph.parent.mkdir(parents=True)
    graph.write_bytes(b"original")
    monkeypatch.setattr(prepare_speech_models, "TTS_FRAME_SHA256", prepare_speech_models.sha256(graph))
    profile = tmp_path / "tts-profile.json"
    profile.write_text('{"precision":"fp32"}')

    def fail(*args):
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(quantize_zerotts, "quantize_zerotts", fail)
    with pytest.raises(RuntimeError, match="conversion failed"):
        prepare_speech_models.prepare(tmp_path, "transformers", "int8", local_only=True)
    assert json.loads(profile.read_text()) == {"precision": "fp32"}


def test_folded_shared_weights_are_quantized_and_inference_remains_valid(tmp_path):
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper

    source, destination = tmp_path / "zerotts", tmp_path / "zerotts-int8"
    (source / "onnx/codec").mkdir(parents=True)
    (source / "onnx/codec/data.bin").write_bytes(b"original codec")
    weights = np.linspace(-1, 1, 16, dtype=np.float32).reshape(4, 4)
    graph = helper.make_graph(
        [
            helper.make_node("Identity", ["weight"], ["shared_weight"]),
            helper.make_node("MatMul", ["x", "shared_weight"], ["y"]),
        ],
        "shared_weights",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 4])],
        initializer=[numpy_helper.from_array(weights, "weight")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    for name in quantize_zerotts.GRAPHS:
        onnx.save(model, str(source / "onnx" / name))
    before = quantize_zerotts.artifact_hashes(source)
    manifest = quantize_zerotts.quantize_zerotts(source, destination, "test-revision")
    assert quantize_zerotts.artifact_hashes(source) == before
    assert (destination / "onnx/codec/data.bin").read_bytes() == b"original codec"
    for name in quantize_zerotts.GRAPHS:
        assert manifest["graphs"][name]["int8_matmul_nodes"] == 1
        session = ort.InferenceSession(str(destination / "onnx" / name), providers=["CPUExecutionProvider"])
        x = np.array([[0.25, -0.5, 0.75, 1]], dtype=np.float32)
        np.testing.assert_allclose(session.run(None, {"x": x})[0], x @ weights, atol=0.025)
    stamp = (destination / "quantization.json").stat().st_mtime_ns
    assert quantize_zerotts.quantize_zerotts(source, destination, "test-revision") == manifest
    assert (destination / "quantization.json").stat().st_mtime_ns == stamp
    # A damaged output must be rebuilt, not silently reused.
    (destination / "onnx/codec/data.bin").write_bytes(b"damaged")
    quantize_zerotts.quantize_zerotts(source, destination, "test-revision")
    assert (destination / "onnx/codec/data.bin").read_bytes() == b"original codec"


def test_failed_rebuild_keeps_previous_artifact(tmp_path, monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    pytest.importorskip("onnx")
    source, destination = tmp_path / "source", tmp_path / "zerotts-int8"
    source.mkdir()
    destination.mkdir()
    saved = destination / "previous-model"
    saved.write_bytes(b"working model")

    def fail(*args, **kwargs):
        raise RuntimeError("unsupported graph")

    monkeypatch.setattr(ort, "InferenceSession", fail)
    with pytest.raises(RuntimeError, match="unsupported graph"):
        quantize_zerotts.quantize_zerotts(source, destination, "test")
    assert saved.read_bytes() == b"working model"
    assert not list(tmp_path.glob(".zerotts-int8-*"))
