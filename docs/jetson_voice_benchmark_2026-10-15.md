# Jetson voice pipeline benchmark

Device: Jetson AGX Xavier 16 GB, JetPack/L4T R35, CUDA 11.8, compute capability 7.2. The candidate ran from `feat/jetson-realtime-voice-pipeline` on staging ports 8795/1249/1250, with model artifacts read from the existing data directory and logs isolated under `/mnt/jetson-data/vivi-ai-staging`.

## Before the pipeline change

Three concurrent Qwen + Whisper requests used a short synthetic Qwen prompt (350 characters, 64 output tokens) and a one-second silent STT WAV:

| Round | STT wall time | Qwen wall time |
|---|---:|---:|
| 1 | 17.352 s | 17.356 s |
| 2 | 4.145 s | 4.148 s |
| 3 | 0.694 s | 0.898 s |

These figures capture cold start and shared-load effects. They are not comparable to the real planner request because the prompt and output limit were much smaller.

## After the pipeline change

The same one-second silent WAV, sent three times sequentially through the staged gateway, completed in 7.609 s, 0.387 s and 0.312 s (median 0.387 s). The first request includes cold work.

TTS used the same Vietnamese sentence for each profile. Times below are first streamed audio chunk / complete streamed audio, three sequential runs:

| Profile | Model load | Run 1 | Run 2 | Run 3 | Warm median first chunk / total |
|---|---:|---:|---:|---:|---:|
| fp32 CUDA | 26.439 s | 1.038 / 5.547 s | 0.168 / 4.848 s | 0.161 / 4.798 s | 0.165 / 4.823 s |
| INT8 CPU | 15.932 s | 2.163 / 11.428 s | 0.574 / 7.104 s | 0.468 / 6.620 s | 0.521 / 6.862 s |
| INT8 GPU TensorRT | deferred | not run | not run | not run | not measured |

Fp32 loaded all five ONNX sessions on CUDA and used about 2.45 GiB RSS. INT8 CPU loaded all five sessions on `CPUExecutionProvider` and used about 0.73 GiB RSS. Its warm first chunk was about 3.1 times slower and the complete utterance about 1.4 times slower than fp32. TTS INT8 GPU was deferred by the memory guard when `MemAvailable` was 8,149 MB, below the 8,192 MB needed to preserve the configured 2 GiB reserve; it was not forced to load.

The service default is fp32. CPU INT8 remains an explicit lower-memory profile. GPU INT8 is not a usable default while the SLM and STT are resident under this memory reserve.

## SLM prompt measurement

The pre-change figures above do not measure the real planner prompt. A follow-up benchmark was prepared to use the application persona, semantic instructions, capability schema and a representative conversation context, with the configured 2,048-token output limit. Automatic review blocked pushing the generated fixture because it contains the full internal prompt and capability catalog. No full-prompt Qwen timing is claimed here.
