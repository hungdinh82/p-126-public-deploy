import numpy as np
import pytest

from jetson_ai.trt_linear import prepare_linear_graph
from scripts.quantize_zerotts import file_hash


@pytest.mark.parametrize("shape", [(1, 8), (2, 8), (1, 11, 8), (2, 1, 8)])
def test_qdq_conv_layout_preserves_outputs_and_calibrated_sources(tmp_path, shape):
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper

    source = tmp_path / "source/model.onnx"
    source.parent.mkdir()
    rng = np.random.default_rng(42)
    weights = rng.integers(-127, 128, size=(8, 12), dtype=np.int8)
    tensors = [
        numpy_helper.from_array(weights, "weight"),
        numpy_helper.from_array(np.full(12, 0.01, dtype=np.float32), "w_scale"),
        numpy_helper.from_array(np.zeros(12, dtype=np.int8), "w_zero"),
        numpy_helper.from_array(np.array(0.05, dtype=np.float32), "a_scale"),
        numpy_helper.from_array(np.array(0, dtype=np.int8), "a_zero"),
    ]
    graph = helper.make_graph(
        [
            helper.make_node("QuantizeLinear", ["x", "a_scale", "a_zero"], ["x_q"]),
            helper.make_node("DequantizeLinear", ["x_q", "a_scale", "a_zero"], ["x_dq"]),
            helper.make_node("DequantizeLinear", ["weight", "w_scale", "w_zero"], ["w_dq"], axis=1),
            helper.make_node("MatMul", ["x_dq", "w_dq"], ["y"]),
        ],
        "linear",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [None] * (len(shape) - 1) + [8])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [None] * (len(shape) - 1) + [12])],
        tensors,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    onnx.save_model(
        model,
        str(source),
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location="weights.data",
        size_threshold=0,
    )
    original_hash, weights_hash = file_hash(source), file_hash(source.parent / "weights.data")
    destination = prepare_linear_graph(source, tmp_path / "cache/model.onnx")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    original = ort.InferenceSession(str(source), sess_options=options, providers=["CPUExecutionProvider"])
    patched = ort.InferenceSession(str(destination), sess_options=options, providers=["CPUExecutionProvider"])
    x = rng.normal(size=shape).astype(np.float32)
    expected = original.run(None, {"x": x})[0]
    actual = patched.run(None, {"x": x})[0]
    assert actual.shape == expected.shape
    np.testing.assert_allclose(actual, expected, atol=2e-6, rtol=2e-6)
    converted = onnx.load(str(destination))
    conv_weight = next(t for t in converted.graph.initializer if t.name.endswith("_weight"))
    assert conv_weight.data_type == TensorProto.INT8
    np.testing.assert_array_equal(numpy_helper.to_array(conv_weight), weights.T.reshape(12, 8, 1, 1))
    assert file_hash(source) == original_hash
    assert file_hash(source.parent / "weights.data") == weights_hash
    timestamp = destination.stat().st_mtime_ns
    prepare_linear_graph(source, destination)
    assert destination.stat().st_mtime_ns == timestamp
    destination.write_bytes(b"corrupt")
    prepare_linear_graph(source, destination)
    assert file_hash(source) == original_hash
    assert file_hash(source.parent / "weights.data") == weights_hash
