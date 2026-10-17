import json

import pytest

from jetson_ai.tts_profile import resolve_tts_model
from scripts import convert_zerotts_fp16, prepare_speech_models


def test_fp16_is_explicit_and_fp32_override_recovers_original(tmp_path):
    base = tmp_path / "zerotts"
    assert resolve_tts_model(base) == (base, "fp32")
    with pytest.raises(FileNotFoundError, match="FP16 model missing"):
        resolve_tts_model(base, "fp16")
    output = tmp_path / "zerotts-fp16"
    output.mkdir()
    manifest = output / "quantization.json"
    manifest.write_text(json.dumps({"format": "float16-mixed", "execution_provider": "CUDAExecutionProvider"}))
    assert resolve_tts_model(base, "fp16") == (output, "float16-mixed")
    assert resolve_tts_model(output) == (output, "float16-mixed")
    assert resolve_tts_model(output, "fp32") == (base, "fp32")
    manifest.write_text('{"format":"float16-mixed","execution_provider":"CPUExecutionProvider"}')
    with pytest.raises(ValueError, match="Unsupported ZeroTTS FP16"):
        resolve_tts_model(output)


def test_fp16_conversion_failure_preserves_active_profile(tmp_path, monkeypatch):
    (tmp_path / "whisper").mkdir()
    graph = tmp_path / "zerotts/onnx/local_frame_decode.onnx"
    graph.parent.mkdir(parents=True)
    graph.write_bytes(b"original")
    monkeypatch.setattr(prepare_speech_models, "TTS_FRAME_SHA256", prepare_speech_models.sha256(graph))
    profile = tmp_path / "tts-profile.json"
    profile.write_text('{"precision":"fp32"}')

    def fail(*args):
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(convert_zerotts_fp16, "convert", fail)
    with pytest.raises(RuntimeError, match="conversion failed"):
        prepare_speech_models.prepare(tmp_path, "whisper_cpp", "fp16", local_only=True)
    assert json.loads(profile.read_text()) == {"precision": "fp32"}
    assert graph.read_bytes() == b"original"


def test_failed_graph_validation_preserves_previous_artifact(tmp_path, monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    source = tmp_path / "zerotts"
    (source / "onnx").mkdir(parents=True)
    graph = helper.make_graph(
        [helper.make_node("Identity", ["x"], ["y"])],
        "io",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1])],
    )
    onnx.save(helper.make_model(graph), str(source / "onnx/text_encoder.onnx"))
    destination = tmp_path / "zerotts-fp16"
    destination.mkdir()
    (destination / "working").write_bytes(b"keep")
    monkeypatch.setattr(ort, "get_available_providers", lambda: ["CUDAExecutionProvider"])

    def fail(*args, **kwargs):
        raise RuntimeError("CUDA graph validation failed")

    monkeypatch.setattr(ort, "InferenceSession", fail)
    before = convert_zerotts_fp16.artifact_hashes(source)
    with pytest.raises(RuntimeError, match="CUDA graph validation failed"):
        convert_zerotts_fp16.convert(source, destination)
    assert convert_zerotts_fp16.artifact_hashes(source) == before
    assert (destination / "working").read_bytes() == b"keep"
    assert not list(tmp_path.glob(".zerotts-fp16-*"))
