// Read actual TensorRT engine layer formats without Python TensorRT bindings.
#include <NvInfer.h>
#include <NvInferPlugin.h>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <vector>

class Logger final : public nvinfer1::ILogger {
 public:
  void log(Severity severity, char const* message) noexcept override {
    if (severity <= Severity::kWARNING) std::cerr << message << '\n';
  }
};

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "Usage: trt-inspect engine-file [engine-file ...]\n";
    return 2;
  }
  Logger logger;
  if (!initLibNvInferPlugins(&logger, "")) return 3;
  std::unique_ptr<nvinfer1::IRuntime> runtime(nvinfer1::createInferRuntime(logger));
  if (!runtime) return 5;
  if (argc > 2) std::cout << '[';
  for (int i = 1; i < argc; ++i) {
    std::ifstream file(argv[i], std::ios::binary);
    if (!file) return 4;
    std::vector<char> bytes{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    std::unique_ptr<nvinfer1::ICudaEngine> engine(runtime->deserializeCudaEngine(bytes.data(), bytes.size()));
    if (!engine) return 6;
    // Without a context, TRT reports "N/A due to dynamic shapes" for formats.
    // Bind the profile's actual optimum dimensions before inspecting precision;
    // no inference or scratch device-memory allocation is needed here.
    std::unique_ptr<nvinfer1::IExecutionContext> context(engine->createExecutionContextWithoutDeviceMemory());
    if (!context) return 9;
    for (int binding = 0; binding < engine->getNbIOTensors(); ++binding) {
      auto name = engine->getIOTensorName(binding);
      if (engine->getTensorIOMode(name) != nvinfer1::TensorIOMode::kINPUT) continue;
      auto dimensions = engine->getTensorShape(name);
      bool dynamic = false;
      for (int d = 0; d < dimensions.nbDims; ++d) dynamic = dynamic || dimensions.d[d] < 0;
      if (dynamic && !context->setInputShape(name, engine->getProfileShape(name, 0, nvinfer1::OptProfileSelector::kOPT))) {
        return 10;
      }
    }
    std::unique_ptr<nvinfer1::IEngineInspector> inspector(engine->createEngineInspector());
    if (!inspector) return 7;
    inspector->setExecutionContext(context.get());
    auto information = inspector->getEngineInformation(nvinfer1::LayerInformationFormat::kJSON);
    if (!information) return 8;
    if (i > 1) std::cout << ',';
    std::cout << information;
  }
  if (argc > 2) std::cout << ']';
  std::cout << '\n';
}
