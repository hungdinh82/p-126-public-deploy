"""Build a separate, validated CPU INT8 variant of the pinned ZeroTTS graphs."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

GRAPHS = ("text_encoder.onnx", "prefix_step.onnx", "local_frame_decode.onnx")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): file_hash(path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".cache" not in path.relative_to(root).parts
            and path.name != "quantization.json"}


def iter_nodes(graph):
    for node in graph.node:
        yield node
        for attribute in node.attribute:
            if attribute.HasField("g"):
                yield from iter_nodes(attribute.g)
            for child in attribute.graphs:
                yield from iter_nodes(child)


def quantize_zerotts(source: Path, destination: Path, revision: str) -> dict:
    import onnx
    import onnxruntime as ort
    from onnxruntime.quantization import QuantType, quantize_dynamic

    if source.resolve() == destination.resolve():
        raise ValueError("INT8 output must be separate from the FP32 model")
    settings = {"format": "int8-dynamic-mixed", "execution_provider": "CPUExecutionProvider",
                "source_revision": revision, "onnx_version": onnx.__version__,
                "ort_version": ort.__version__, "op_types": ["MatMul"], "per_channel": False,
                # Avoid U8S8 accumulator saturation on x86 AVX2 CPUs used for
                # this experiment. Storage remains INT8; weight range is 7-bit.
                "reduce_range": True,
                "preprocess": "ort-basic-identity-fold",
                "source_sha256": artifact_hashes(source)}
    manifest_path = destination / "quantization.json"
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text())
        if (all(previous.get(key) == value for key, value in settings.items())
                and previous.get("output_sha256") == artifact_hashes(destination)):
            print(f"Reusing verified INT8 model: {destination}", flush=True)
            return previous

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".zerotts-int8-", dir=destination.parent) as temp:
        stage = Path(temp) / "model"
        # The three pinned transformer graphs have inline weights. The codec's
        # shared external data is copied unchanged along with its FP32 graphs.
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns(".cache", *GRAPHS))
        reports = {}
        for name in GRAPHS:
            original = source / "onnx" / name
            output = stage / "onnx" / name
            print(f"Quantizing ZeroTTS {name} (CPU INT8 MatMul)...", flush=True)
            # Follow ZeroTTS tools/quantize_onnx_int8.py: shared weights often
            # pass through Identity, hiding them from the dynamic quantizer.
            folded = Path(temp) / f"{name}.opt"
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            options.optimized_model_filepath = str(folded)
            session = ort.InferenceSession(str(original), sess_options=options,
                                           providers=["CPUExecutionProvider"])
            del session
            try:
                quantize_dynamic(str(folded), str(output), weight_type=QuantType.QInt8,
                                 reduce_range=True,
                                 op_types_to_quantize=["MatMul"], use_external_data_format=True,
                                 extra_options={"MatMulConstBOnly": True, "EnableSubgraph": True})
            finally:
                folded.unlink(missing_ok=True)
            graph = onnx.load(str(output), load_external_data=False)
            count = sum(node.op_type == "MatMulInteger" for node in iter_nodes(graph.graph))
            if not count:
                raise RuntimeError(f"No INT8 MatMul operators produced in {name}")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            session = ort.InferenceSession(str(output), sess_options=options,
                                           providers=["CPUExecutionProvider"])
            del session
            reports[name] = {"int8_matmul_nodes": count}
        manifest = {**settings, "graphs": reports, "codec_precision": "fp32",
                    "output_sha256": artifact_hashes(stage)}
        (stage / "quantization.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Publish only after all converted graphs load successfully. A failed
        # rebuild leaves the previously working INT8 artifact in place.
        backup = Path(temp) / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
    return manifest
