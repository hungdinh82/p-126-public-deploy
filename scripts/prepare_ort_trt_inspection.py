"""Enable engine metadata and dynamic shapes for JetPack TensorRT 8.5.2."""

from pathlib import Path

ORIGINAL = "auto trt_config = std::unique_ptr<nvinfer1::IBuilderConfig>(trt_builder->createBuilderConfig());"
FIXED = ORIGINAL + "\n      trt_config->setProfilingVerbosity(nvinfer1::ProfilingVerbosity::kDETAILED);"
DYNAMIC_FIXED = FIXED + """
#if NV_TENSORRT_MAJOR == 8 && NV_TENSORRT_MINOR == 5
      trt_config->setPreviewFeature(nvinfer1::PreviewFeature::kFASTER_DYNAMIC_SHAPES_0805, true);
#endif"""
HARDWARE_ORIGINAL = "trt_config->setHardwareCompatibilityLevel(nvinfer1::HardwareCompatibilityLevel::kAMPERE_PLUS);"
HARDWARE_FIXED = """#if NV_TENSORRT_MAJOR > 8 || (NV_TENSORRT_MAJOR == 8 && NV_TENSORRT_MINOR >= 6)
    trt_config->setHardwareCompatibilityLevel(nvinfer1::HardwareCompatibilityLevel::kAMPERE_PLUS);
#else
    return ORT_MAKE_STATUS(ONNXRUNTIME, EP_FAIL, "Hardware compatibility requires TensorRT 8.6 or newer");
#endif"""
CAPABILITY_ORIGINAL = """  bool early_termination = false;
  supported_nodes_vector = GetSupportedList(parser_nodes_vector, 0, max_partition_iterations_, graph, &early_termination);"""
CAPABILITY_FIXED = """  // Opt-in Xavier QDQ linear kernels. CUDA handles attention, shapes and sampling.
  // Seed each Conv together with its two DQ inputs, so constant weights cannot
  // form separate FP32 engines and dynamic KV axes never reach the TRT builder.
  if (onnxruntime::GetEnvironmentVar("VIVI_TRT_INT8_LINEAR_ONLY") == "1") {
    parser_nodes_vector.clear();
    std::unordered_map<NodeIndex, size_t> positions;
    for (size_t i = 0; i < node_index.size(); ++i) positions[node_index[i]] = i;
    for (const auto& index : nodes_vector) {
      const auto* node = graph.GetNode(node_index[index]);
      if (node->OpType() != "Conv" || node->InputDefs().size() != 2) continue;
      std::vector<size_t> group;
      for (const auto* input : node->InputDefs()) {
        const auto* producer = graph.GetProducerNode(input->Name());
        if (producer == nullptr || producer->OpType() != "DequantizeLinear" ||
            producer->GetOutputEdgesCount() != 1) break;
        group.push_back(positions.at(producer->Index()));
      }
      if (group.size() != 2) continue;
      group.push_back(index);
      std::sort(group.begin(), group.end());
      parser_nodes_vector.push_back({group, false});
    }
  }
""" + CAPABILITY_ORIGINAL
CONTEXT_ORIGINAL = """    // Set execution context memory
    if (trt_state->context_memory_sharing_enable) {"""
CONTEXT_FIXED = """    // Keep borrowed device memory alive through enqueue and stream synchronization.
    std::shared_ptr<void> shared_context_memory;
""" + CONTEXT_ORIGINAL
BUFFER_ORIGINAL = "trt_context->setDeviceMemory(IAllocator::MakeUniquePtrFromOrtAllocator<void>(alloc, *max_context_mem_size_ptr).get());"
BUFFER_LIFETIME_FIXED = """shared_context_memory = IAllocator::MakeUniquePtrFromOrtAllocator<void>(alloc, *max_context_mem_size_ptr);
      trt_context->setDeviceMemory(shared_context_memory.get());"""
BUFFER_FIXED = """shared_context_memory = IAllocator::MakeUniquePtrFromOrtAllocator<void>(alloc, *max_context_mem_size_ptr + 511);
      const uintptr_t aligned_context_memory =
          (reinterpret_cast<uintptr_t>(shared_context_memory.get()) + 511) & ~static_cast<uintptr_t>(511);
      trt_context->setDeviceMemory(reinterpret_cast<void*>(aligned_context_memory));"""


def prepare(path: Path):
    data = path.read_text()
    inspection_count = data.count(FIXED)
    compatibility_count = data.count(HARDWARE_FIXED)
    dynamic_count = data.count(DYNAMIC_FIXED)
    linear_count = data.count(CAPABILITY_FIXED)
    context_count = data.count(CONTEXT_FIXED)
    aligned_count = data.count(BUFFER_FIXED)
    lifetime_count = data.count(BUFFER_LIFETIME_FIXED)
    if (data.count(ORIGINAL) != 2 or inspection_count not in (0, 2)
            or data.count("setProfilingVerbosity") != inspection_count
            or data.count(HARDWARE_ORIGINAL) != 2 or compatibility_count not in (0, 2)
            or dynamic_count not in (0, 2) or data.count("setPreviewFeature") != dynamic_count
            or data.count(CAPABILITY_ORIGINAL) != 1 or linear_count not in (0, 1)
            or data.count("VIVI_TRT_INT8_LINEAR_ONLY") != linear_count
            or data.count(CONTEXT_ORIGINAL) != 2 or context_count not in (0, 2)
            or data.count(BUFFER_ORIGINAL) != 2 - context_count
            or aligned_count not in (0, 2) or lifetime_count not in (0, 2)
            or aligned_count + lifetime_count != context_count):
        raise ValueError("Unexpected ORT TensorRT builder source; inspect before patching")
    if (inspection_count == 2 and compatibility_count == 2 and dynamic_count == 2
            and linear_count == 1 and context_count == 2 and aligned_count == 2):
        return
    backup = path.with_name(path.name + ".before-trt-inspection")
    if not backup.exists():
        backup.write_text(data)
    dynamic_backup = path.with_name(path.name + ".before-trt-dynamic-shapes")
    if dynamic_count == 0 and not dynamic_backup.exists():
        dynamic_backup.write_text(data)
    if inspection_count == 0:
        data = data.replace(ORIGINAL, FIXED)
    if compatibility_count == 0:
        data = data.replace(HARDWARE_ORIGINAL, HARDWARE_FIXED)
    if dynamic_count == 0:
        data = data.replace(FIXED, DYNAMIC_FIXED)
    if linear_count == 0:
        data = data.replace(CAPABILITY_ORIGINAL, CAPABILITY_FIXED)
    if context_count == 0:
        data = data.replace(CONTEXT_ORIGINAL, CONTEXT_FIXED).replace(BUFFER_ORIGINAL, BUFFER_FIXED)
    elif lifetime_count == 2:
        data = data.replace(BUFFER_LIFETIME_FIXED, BUFFER_FIXED)
    path.write_text(data)


if __name__ == "__main__":
    import sys
    prepare(Path(sys.argv[1]))
