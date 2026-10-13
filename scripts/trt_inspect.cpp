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
  if (argc != 2) {
    std::cerr << "Usage: trt-inspect engine-file\n";
    return 2;
  }
  Logger logger;
  if (!initLibNvInferPlugins(&logger, "")) return 3;
  std::ifstream file(argv[1], std::ios::binary);
  if (!file) return 4;
  std::vector<char> bytes{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
  std::unique_ptr<nvinfer1::IRuntime> runtime(nvinfer1::createInferRuntime(logger));
  if (!runtime) return 5;
  std::unique_ptr<nvinfer1::ICudaEngine> engine(runtime->deserializeCudaEngine(bytes.data(), bytes.size()));
  if (!engine) return 6;
  std::unique_ptr<nvinfer1::IEngineInspector> inspector(engine->createEngineInspector());
  if (!inspector) return 7;
  auto information = inspector->getEngineInformation(nvinfer1::LayerInformationFormat::kJSON);
  if (!information) return 8;
  std::cout << information << '\n';
}
