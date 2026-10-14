"""Enable engine metadata and guard TRT 8.6 APIs for JetPack TRT 8.5.2."""

from pathlib import Path

ORIGINAL = "auto trt_config = std::unique_ptr<nvinfer1::IBuilderConfig>(trt_builder->createBuilderConfig());"
FIXED = ORIGINAL + "\n      trt_config->setProfilingVerbosity(nvinfer1::ProfilingVerbosity::kDETAILED);"
HARDWARE_ORIGINAL = "trt_config->setHardwareCompatibilityLevel(nvinfer1::HardwareCompatibilityLevel::kAMPERE_PLUS);"
HARDWARE_FIXED = """#if NV_TENSORRT_MAJOR > 8 || (NV_TENSORRT_MAJOR == 8 && NV_TENSORRT_MINOR >= 6)
    trt_config->setHardwareCompatibilityLevel(nvinfer1::HardwareCompatibilityLevel::kAMPERE_PLUS);
#else
    return ORT_MAKE_STATUS(ONNXRUNTIME, EP_FAIL, "Hardware compatibility requires TensorRT 8.6 or newer");
#endif"""


def prepare(path: Path):
    data = path.read_text()
    inspection_count = data.count(FIXED)
    compatibility_count = data.count(HARDWARE_FIXED)
    if (data.count(ORIGINAL) != 2 or inspection_count not in (0, 2)
            or data.count("setProfilingVerbosity") != inspection_count
            or data.count(HARDWARE_ORIGINAL) != 2 or compatibility_count not in (0, 2)):
        raise ValueError("Unexpected ORT TensorRT builder source; inspect before patching")
    if inspection_count == 2 and compatibility_count == 2:
        return
    backup = path.with_name(path.name + ".before-trt-inspection")
    if not backup.exists():
        backup.write_text(data)
    if inspection_count == 0:
        data = data.replace(ORIGINAL, FIXED)
    if compatibility_count == 0:
        data = data.replace(HARDWARE_ORIGINAL, HARDWARE_FIXED)
    path.write_text(data)


if __name__ == "__main__":
    import sys
    prepare(Path(sys.argv[1]))
