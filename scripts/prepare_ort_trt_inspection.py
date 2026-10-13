"""Enable TensorRT engine metadata in the two pinned ORT builder paths."""

from pathlib import Path

ORIGINAL = "auto trt_config = std::unique_ptr<nvinfer1::IBuilderConfig>(trt_builder->createBuilderConfig());"
FIXED = ORIGINAL + "\n      trt_config->setProfilingVerbosity(nvinfer1::ProfilingVerbosity::kDETAILED);"


def prepare(path: Path):
    data = path.read_text()
    if data.count(FIXED) == 2:
        return
    if data.count(ORIGINAL) != 2 or "setProfilingVerbosity" in data:
        raise ValueError("Unexpected ORT TensorRT builder source; inspect before patching")
    backup = path.with_name(path.name + ".before-trt-inspection")
    if not backup.exists():
        backup.write_text(data)
    path.write_text(data.replace(ORIGINAL, FIXED))


if __name__ == "__main__":
    import sys
    prepare(Path(sys.argv[1]))
