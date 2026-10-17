> Báo cáo lịch sử của nhánh main ngày 15/10/2026. Các phần local forms, voice inbox và budget cũ không thuộc runtime v2 hiện tại. Xem [tích hợp hiện tại](JETSON_MAIN_INTEGRATION.md); số đo dưới đây không phải nghiệm thu bản merge.

# ZeroTTS precision comparison on AGX Xavier — 2026-10-15

FP16 CUDA is a useful optional memory compromise: the measured process RSS is
13.3% smaller than FP32, while synthesis speed is effectively unchanged. Keep
FP32 as the default until broader listening and conversational load validation.
INT8 TensorRT is currently slower and substantially larger in RAM on this stack;
it is not suitable as the default. INT8 CPU saves the most process memory, with
higher speech latency.

## Method and measured results

AGX Xavier 16GB (14,886 MiB OS RAM), MAXN mode 0, dynamic clocks, native CUDA11.8 /
ONNX Runtime1.20.1, ZeroTTS0.1.5 and the existing VIVI voice. Existing pinned model
weights and inference backends are retained. Each profile runs alone in a fresh
process through `SpeechModels.synthesize_stream`, with Qwen and Whisper stopped.
Same text: “Mình sẽ giúp bạn điều chỉnh nhiệt độ nhé.” Seed42 for each request.
One cold request is excluded, followed by 30 warm requests. Profiles run
sequentially, without forced clock locking or an alternating randomized order.
Small latency differences should not be interpreted as a demonstrated speedup.

| Profile | First audio median / p95 | Synthesis median / p95 | Generated duration | Median RTF | Process RSS | Profile load |
|---|---:|---:|---:|---:|---:|---:|
| FP32 CUDA | 98.29 / 105.02 ms | 1388.49 / 1447.95 ms | 1760 ms | 0.7890 | 2.85 GiB | 23.74 s |
| FP16 mixed CUDA | 99.45 / 104.14 ms | 1405.89 / 1450.39 ms | 1760 ms | 0.7988 | 2.47 GiB | 13.32 s |
| INT8 CPU | 351.01 / 361.21 ms | 2473.68 / 2732.38 ms | 1920 ms | 1.2883 | 0.89 GiB | 10.56 s |
| INT8 mixed TensorRT | 215.92 / 258.77 ms | 3575.64 / 4353.20 ms | 1840 ms | 1.9432 | 7.15 GiB | 212.85 s |

RSS is the whole benchmark process from `/proc/self/status`, not model-only GPU
allocation; Xavier shares RAM between CPU and GPU. Do not add these values to
GPU memory estimates. FP16 saves 396,876 KiB (387.57 MiB) of process RSS here.
System available memory was 9427 / 9516 / 9472 / 4312 MiB minimum for FP32 /
FP16 / INT8 CPU / INT8 TensorRT respectively; cached pages mean RSS savings do
not translate one-to-one into this system-level metric. Existing swap usage
predates these runs; this is not a claim of a swap-free deployment.

Generation durations differ across precisions despite an identical text and
seed: the autoregressive discrete output can differ. RTF normalizes synthesis
cost by produced audio duration. Every benchmark output is nonempty, nonzero
and finite; this does not establish perceptual equivalence or intelligibility
across an unrestricted production corpus.

## Precision and implementation

FP16 converts only `text_encoder`, `prefix_step` and `local_frame_decode` with
the converter bundled in the installed ONNX Runtime. Unsupported operators
remain FP32; float32 public inputs/outputs and both FP32 codec graphs are
preserved. Original artifacts are never overwritten. Conversion validates all
three sessions with CUDA before atomically publishing `zerotts-fp16`. A manifest
records versions, source/output hashes and conversion settings. Reuse requires
all hashes to match. This is an explicit CUDA profile, with no whole-session CPU
replacement; small shape/operator CPU partitions remain possible, as with FP32.
See [ONNX Runtime float16 guidance](https://onnxruntime.ai/docs/performance/model-optimizations/float16.html).

INT8 TensorRT execution was inspected, rather than inferred from a provider
name: 72 / 63 / 425 INT8 compute entries in text encoder / prefix / frame graphs.
Codec stays FP32 CUDA, and unconverted partitions retain CUDA/CPU execution.
Cached TensorRT startup still took 212.85s including engine verification and
codec promotion. The engine emitted a device portability warning; execution
verification nevertheless passed. No full Qwen/STT/TensorRT residency test was
forced because the measured RAM footprint leaves too little headroom. The
existing 2GiB memory reserve remains enforced.

## Reproduce

From the Jetson staging checkout, prepare FP16 once without selecting a new
default or downloading another model:

```bash
export JETSON_AI_DATA_DIR=/mnt/jetson-data/vivi-ai-staging
source scripts/jetson_profile.sh
jetson_activate_sdk
export PYTHONPATH=.
"$VIVI_VENV/bin/python" scripts/prepare_speech_models.py \
  --root "$JETSON_AI_DATA_DIR/models/speech" --engine whisper_cpp \
  --local-only --defer-selection --tts-precision fp16
```

To benchmark, stop only the owned staging server first so it does not retain
other models, and run each profile separately:

```bash
export JETSON_AI_TTS_MODEL="$JETSON_AI_DATA_DIR/models/speech/zerotts"
export JETSON_AI_DEVICE=cuda
export JETSON_AI_TTS_PRECISION=fp16 # fp32, int8, or int8-gpu
export JETSON_AI_TRT_INSPECTOR="$JETSON_AI_DATA_DIR/build/trt-inspect"
"$VIVI_VENV/bin/python" scripts/benchmark_jetson_tts_profile.py \
  --rounds 30 --output /tmp/tts-profile.json
```

To run the server with the already prepared FP16 artifact:

```bash
JETSON_AI_DATA_DIR=/mnt/jetson-data/vivi-ai-staging \
JETSON_AI_PORT=8795 JETSON_AI_BACKEND_PORT=1249 JETSON_AI_STT_PORT=1250 \
JETSON_AI_BIND=100.123.27.125 \
bash scripts/jetson_ai.sh run --tts-precision fp16
```

Plain `run` still defaults to FP32. FP16 does not change Qwen's Q4_K_M weights
or Whisper's model/precision. Raw measurements: `jetson_tts_profiles_2026-10-15.json`.

## HTTP smoke with all three models resident

Started the staging launcher with `--tts-precision fp16` on8795, native Qwen on1249
and Whisper on1250. All three capabilities reported ready and CUDA. Three HTTP
streaming requests returned nonempty PCM; the first cold request took687ms to
first bytes, subsequent requests140ms and116ms. Qwen was resident but idle: this
is not a simultaneous GPU inference stress test. System available RAM after
these requests was5541MiB, with preexisting1090MiB swap usage unchanged.

Whisper transcribed the window question correctly, while the temperature
sentences contained recognition substitutions. This round trip alone cannot
attribute errors to FP16 versus the existing TTS/Whisper behavior, and no paired
listening/recognition quality comparison has been established. Audio samples
remain on Jetson at `/tmp/vivi-fp16-sample-{0,1,2}.wav` for listening. FP32 remains
the default; staging is restored to the previously selected INT8 CPU profile.

Validation:29 focused tests passed for FP16/CPU INT8/TensorRT artifact selection,
failed-conversion recovery and memory guards; Ruff and shell syntax passed.
