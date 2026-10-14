"""Cache-local QDQ linear layout for Xavier's native INT8 convolution kernels."""

import json
from pathlib import Path

from scripts.quantize_zerotts import file_hash

LINEAR_RECIPE = "qdq-conv1x1-v2"


def prepare_linear_graph(source: Path, destination: Path) -> Path:
    """Express constant-weight QDQ MatMul as Conv, preserving calibrated values."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    stamp = destination.with_suffix(".compat.json")
    weights_path = destination.parent / (destination.stem + ".linear.data")
    source_hash = file_hash(source)
    if stamp.is_file() and destination.is_file():
        previous = json.loads(stamp.read_text())
        if (weights_path.is_file()
                and previous == {"recipe": LINEAR_RECIPE, "source_sha256": source_hash,
                                 "output_sha256": file_hash(destination),
                                 "weights_sha256": file_hash(weights_path)}):
            return destination

    model = onnx.load(str(source), load_external_data=False)
    graph = model.graph
    initializers = {value.name: value for value in graph.initializer}
    producers = {name: node for node in graph.node for name in node.output}
    prefix = "vivi_int8_linear_"
    names = {name for node in graph.node for name in (*node.input, *node.output)}
    if any(name.startswith(prefix) for name in names):
        raise ValueError("Graph already contains compatibility tensor names")
    nodes, added = [], []
    converted = 0

    def constant(name, values):
        added.append(helper.make_tensor(name, TensorProto.INT64, [len(values)], values))
        return name

    for index, node in enumerate(graph.node):
        inputs = [producers.get(name) for name in node.input]
        if (node.op_type != "MatMul" or len(inputs) != 2
                or any(value is None or value.op_type != "DequantizeLinear" for value in inputs)):
            nodes.append(node)
            continue
        activation, weight_dq = inputs
        weight = initializers.get(weight_dq.input[0])
        if weight is None or weight.data_type != TensorProto.INT8:
            nodes.append(node)
            continue
        axis = next((attr.i for attr in weight_dq.attribute if attr.name == "axis"), 1)
        scale = initializers.get(weight_dq.input[1])
        if len(weight.dims) != 2 or axis != 1 or scale is None:
            raise ValueError("Unexpected calibrated MatMul weight layout")
        # Activation calibration is per-tensor; Conv weights are per-output
        # channel. No new quantization or calibration is performed here.
        activation_scale = initializers.get(activation.input[1])
        if activation_scale is None or np.prod(activation_scale.dims) != 1:
            raise ValueError("Expected per-tensor activation scale")
        k, n = weight.dims
        tag = f"{prefix}{index}_"
        copy = TensorProto()
        copy.CopyFrom(weight)
        array = numpy_helper.to_array(copy, base_dir=str(source.parent))
        conv_weight = tag + "weight"
        added.append(numpy_helper.from_array(np.ascontiguousarray(array.T).reshape(n, k, 1, 1), conv_weight))
        input_shape = constant(tag + "input_shape", [-1, k, 1, 1])
        last_dim = constant(tag + "last_dim", [n])
        starts = constant(tag + "starts", [0])
        ends = constant(tag + "ends", [-1])
        axes = constant(tag + "axes", [0])
        nodes.extend([
            helper.make_node("Shape", [node.input[0]], [tag + "original_shape"]),
            helper.make_node("Slice", [tag + "original_shape", starts, ends, axes], [tag + "leading_shape"]),
            helper.make_node("Concat", [tag + "leading_shape", last_dim], [tag + "output_shape"], axis=0),
            helper.make_node("Reshape", [activation.input[0], input_shape], [tag + "quantized_input"]),
            helper.make_node("DequantizeLinear", [tag + "quantized_input", *activation.input[1:]],
                             [tag + "input"]),
            helper.make_node("DequantizeLinear", [conv_weight, *weight_dq.input[1:]],
                             [tag + "filter"], axis=0),
            helper.make_node("Conv", [tag + "input", tag + "filter"], [tag + "convolution"],
                             name=tag + "conv", kernel_shape=[1, 1]),
            helper.make_node("Reshape", [tag + "convolution", tag + "output_shape"], list(node.output)),
        ])
        converted += 1
    if not converted:
        raise ValueError("No calibrated INT8 MatMul found")

    # Remove superseded DQ nodes and unused weights. Keep I/O and all live
    # attention/sampling nodes, including their original boolean masks.
    needed = {value.name for value in graph.output}
    live = []
    for node in reversed(nodes):
        if any(name in needed for name in node.output):
            live.append(node)
            needed.update(node.input)
    del graph.node[:]
    graph.node.extend(reversed(live))
    kept = [value for value in (*graph.initializer, *added) if value.name in needed]
    del graph.initializer[:]
    graph.initializer.extend(kept)
    live_names = needed | {value.name for value in graph.output}
    info = [value for value in graph.value_info if value.name in live_names]
    del graph.value_info[:]
    graph.value_info.extend(info)
    model = onnx.shape_inference.infer_shapes(model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    weights_name = destination.stem + ".linear.data"
    for tensor in model.graph.initializer:
        if tensor.name.startswith(prefix) and tensor.name.endswith("_weight"):
            onnx.external_data_helper.set_external_data(tensor, location=weights_name)
            continue
        location = next((entry.value for entry in tensor.external_data if entry.key == "location"), None)
        if location is None:
            continue
        relative = Path(location)
        if relative.is_absolute() or ".." in relative.parts or relative.name in (destination.name, weights_name):
            raise ValueError("Unexpected external initializer path")
        target = (source.parent / relative).resolve(strict=True)
        link = destination.parent / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            if link.resolve() != target:
                raise ValueError("Compatibility cache points at different weights")
        elif link.exists():
            raise ValueError("Unexpected file in compatibility weight cache")
        else:
            link.symlink_to(target)

    temporary = destination.with_suffix(".onnx.tmp")
    # Only new transposed INT8 arrays have raw_data; original external tensors
    # remain immutable symlinks. Start a new sidecar when repairing the view.
    (destination.parent / weights_name).unlink(missing_ok=True)
    try:
        onnx.save_model(model, str(temporary))
        onnx.checker.check_model(str(temporary))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    stamp.write_text(json.dumps({"recipe": LINEAR_RECIPE, "source_sha256": source_hash,
                                "output_sha256": file_hash(destination),
                                "weights_sha256": file_hash(weights_path)}, indent=2) + "\n")
    return destination
