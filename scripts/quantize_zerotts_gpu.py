"""Calibrate ZeroTTS from real FP32 VIVI synthesis and emit TensorRT INT8 QDQ."""

import json
import os
import random
import shutil
import tempfile
from pathlib import Path

import numpy as np

from scripts.quantize_zerotts import GRAPHS, artifact_hashes, file_hash

CORPUS = (
    "Xin chào bạn, tôi là trợ lý ViVi.",
    "Đặt nhiệt độ điều hòa hai mươi bốn độ.",
    "Vui lòng xác nhận trước khi mở cửa bên tài.",
    "Pin còn tám mươi phần trăm, đủ để đến trạm sạc.",
    "Rẽ phải sau hai trăm mét, rồi đi thẳng đến VinUni.",
    "Kết nối Bluetooth và mở nhạc trên điện thoại.",
)


class FeedRecorder:
    """Bounded reservoir of real graph inputs, including the first cold input."""

    def __init__(self, session, limit=12):
        self.session = session
        self.limit = limit
        self.samples = []
        self.seen = 0
        self.rng = random.Random(42)

    def __getattr__(self, name):
        return getattr(self.session, name)

    def run(self, outputs, feeds, *args, **kwargs):
        result = self.session.run(outputs, feeds, *args, **kwargs)
        self.seen += 1
        index = len(self.samples) if len(self.samples) < self.limit else self.rng.randrange(1, self.seen)
        if index < self.limit:
            sample = {name: np.array(value, copy=True) for name, value in feeds.items()}
            if len(self.samples) < self.limit:
                self.samples.append(sample)
            else:
                self.samples[index] = sample
        return result


class FeedReader:
    def __init__(self, paths):
        self.paths = list(paths)
        self.index = 0

    def get_next(self):
        if self.index == len(self.paths):
            return None
        path = self.paths[self.index]
        self.index += 1
        with np.load(path) as archive:
            return {name: archive[name] for name in archive.files}


def capture_feeds(source: Path, output: Path, corpus: tuple[str, ...], limit: int, voice_pack: Path):
    from jetson_ai.speech import SpeechModels

    os.environ.update(JETSON_AI_TTS_MODEL=str(source), JETSON_AI_TTS_PRECISION="fp32",
                      JETSON_AI_DEVICE="cpu", JETSON_AI_VOICE_PACK=str(voice_pack))
    models = SpeechModels()
    models.load("tts")
    if models.info["tts"]["status"] != "ready":
        raise RuntimeError(models.info["tts"]["error"])
    model, voice = models.models["tts"]
    recorders = {}
    for attribute, name in zip(("text_encoder_sess", "prefix_step_sess", "local_frame_decode_sess"), GRAPHS):
        recorder = FeedRecorder(getattr(model, attribute), limit)
        setattr(model, attribute, recorder)
        recorders[name] = recorder
    for index, text in enumerate(corpus):
        print(f"Calibrating VIVI ({index + 1}/{len(corpus)}): {text}", flush=True)
        np.random.seed(42 + index)
        audio = model.synthesize(text, voice=voice, max_frames=160)
        if not audio.size or not np.isfinite(audio).all() or not np.any(audio):
            raise RuntimeError("Invalid calibration audio")
    report = {}
    for name, recorder in recorders.items():
        folder = output / name.removesuffix(".onnx")
        folder.mkdir(parents=True)
        for index, sample in enumerate(recorder.samples):
            np.savez(folder / f"{index:03d}.npz", **sample)
        report[name] = {"observed_calls": recorder.seen, "saved_feeds": len(recorder.samples)}
    return report


def quantize_graph(source: Path, output: Path, feeds: Path, work: Path) -> dict:
    import onnx
    import onnxruntime as ort
    from onnxruntime.quantization import CalibrationMethod, QuantFormat, QuantType, quantize_static

    folded = work / (source.name + ".opt")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(folded)
    session = ort.InferenceSession(str(source), sess_options=options, providers=["CPUExecutionProvider"])
    del session
    quantize_static(str(folded), str(output), FeedReader(sorted(feeds.glob("*.npz"))),
                    quant_format=QuantFormat.QDQ, activation_type=QuantType.QInt8,
                    weight_type=QuantType.QInt8, per_channel=True,
                    op_types_to_quantize=["MatMul"], calibrate_method=CalibrationMethod.MinMax,
                    use_external_data_format=True,
                    extra_options={"ActivationSymmetric": True, "WeightSymmetric": True,
                                   "MatMulConstBOnly": True, "DedicatedQDQPair": True,
                                   "OpTypesToExcludeOutputQuantization": ["MatMul"]})
    graph = onnx.load(str(output), load_external_data=False).graph
    producers = {value: node for node in graph.node for value in node.output}
    int8_weights = {value.name for value in graph.initializer if value.data_type == onnx.TensorProto.INT8}
    count = 0
    for node in graph.node:
        if node.op_type != "MatMul":
            continue
        inputs = [producers.get(name) for name in node.input]
        if (len(inputs) == 2 and all(n is not None and n.op_type == "DequantizeLinear" for n in inputs)
                and inputs[1].input[0] in int8_weights):
            count += 1
    if not count:
        raise RuntimeError(f"No signed INT8 QDQ MatMul produced for {source.name}")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    # Validate the explicit QDQ arithmetic, not x86 CPU-specific quantized
    # fusions (U8S8 saturation can differ from TensorRT's signed INT8 kernels).
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    session = ort.InferenceSession(str(output), sess_options=options, providers=["CPUExecutionProvider"])
    # Execute real feeds: validates shapes, preserved integer I/O and finite output.
    for path in sorted(feeds.glob("*.npz"))[:2]:
        sample = FeedReader([path]).get_next()
        values = session.run(None, sample)
        if any(not np.isfinite(value).all() for value in values if value.dtype.kind == "f"):
            raise RuntimeError(f"Non-finite calibrated output in {source.name}")
    return {"int8_qdq_matmul_nodes": count}


def quantize_zerotts_gpu(source: Path, destination: Path, revision: str, *,
                        corpus: tuple[str, ...] = CORPUS, limit: int = 12,
                        voice_pack: Path | None = None) -> dict:
    import onnx
    import onnxruntime as ort

    if not corpus or any(not text.strip() for text in corpus) or limit < 2:
        raise ValueError("Calibration needs non-empty texts and at least two feeds per graph")
    voice_pack = voice_pack or Path(__file__).resolve().parents[1] / "voices/VIVI.zip"
    settings = {"format": "int8-qdq-mixed", "execution_provider": "TensorrtExecutionProvider",
                "source_revision": revision, "onnx_version": onnx.__version__, "ort_version": ort.__version__,
                "calibration_texts": list(corpus), "feed_limit": limit, "voice_sha256": file_hash(voice_pack),
                "source_sha256": artifact_hashes(source), "calibration": "minmax-symmetric-s8s8",
                "preprocess": "ort-basic-identity-fold", "recipe_version": 1}
    if source.resolve() == destination.resolve():
        raise ValueError("GPU INT8 output must be separate from FP32 source")
    manifest_path = destination / "quantization.json"
    if manifest_path.is_file():
        previous = json.loads(manifest_path.read_text())
        if (all(previous.get(key) == value for key, value in settings.items())
                and previous.get("output_sha256") == artifact_hashes(destination)):
            print(f"Reusing verified GPU QDQ model: {destination}", flush=True)
            return previous
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".zerotts-int8-gpu-", dir=destination.parent) as temp:
        work = Path(temp)
        stage = work / "model"
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns(".cache", *GRAPHS))
        calibration = work / "calibration"
        coverage = capture_feeds(source, calibration, corpus, limit, voice_pack)
        reports = {}
        for name in GRAPHS:
            print(f"Static INT8 QDQ: {name}", flush=True)
            reports[name] = quantize_graph(source / "onnx" / name, stage / "onnx" / name,
                                          calibration / name.removesuffix(".onnx"), work)
        manifest = {**settings, "graphs": reports, "feed_coverage": coverage, "codec_precision": "fp32",
                    "output_sha256": artifact_hashes(stage)}
        (stage / "quantization.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        backup = work / "previous"
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
    return manifest
