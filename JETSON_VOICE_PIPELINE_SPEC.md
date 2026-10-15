# Implementation Spec: Realtime STT–SLM–TTS on Jetson AGX Xavier

## 1. Context & goal
- Hardware: **one Jetson AGX Xavier**, shared CPU/GPU system RAM; exact RAM size, JetPack version and power mode must be detected.
- Models: **Whisper STT**, **Qwen3-1.7B SLM** (slowest stage, ~10x STT latency), and one fast **Vietnamese TTS**.
- Goal: realtime-feeling voice conversation, always listening, no lost utterances while Qwen is busy, responsive interruption (barge-in), bounded backlog, and no outdated spoken answers.
- **Do not assume** three GPU kernels can run simultaneously or that a fixed GPU percentage can be reserved. Benchmark contention on device.

## 2. Architecture
```text
Mic -> AudioCapture(CPU) -> bounded audio ring buffer -> VAD(CPU)
                                     |                    |
                                     +-> utterance segments -> STT worker (Whisper)
                                                              |
                                               transcript event log (timestamped)
                                                              |
                                               Conversation Manager (CPU)
                                                  | revisioned snapshots
                                                  v
                                             SLM worker (Qwen)
                                                  | sentence chunks
                                                  v
                                             TTS worker -> playback

User speech while playback active -> barge-in controller -> stop playback,
 invalidate queued TTS, collect new transcript, revise pending intent.
```

## 3. Process boundaries and resource policy
- **Audio/VAD/Conversation Manager:** CPU, nonblocking `asyncio` orchestration; audio callback must never await model inference.
- **STT:** one model instance and one worker; Whisper implementation selected after testing `whisper.cpp` versus compatible alternatives. Audio segments are processed in timestamp order; avoid unbounded task creation.
- **SLM:** one Qwen instance, **one active generation**; prefer benchmark of `llama.cpp` + GGUF Q4_K_M, context initially 2048, max output initially 64–128 tokens, disable extended thinking if supported by the actual template/model.
- **TTS:** one worker, synthesize complete sentences/short clauses as Qwen streams; playback independent from inference.
- Models may remain loaded concurrently **only if memory headroom is safe**. Jetson uses shared RAM, not discrete VRAM. Keep a configurable memory reserve (initially >=2 GB) and measure actual RSS/tegrastats.
- Do not enable CUDA MPS, multi-stream tuning or model replicas in v1. Test GPU-STT versus CPU-STT as an optional experiment; keep the fastest end-to-end setup, not merely the fastest isolated STT.

## 4. Data contracts
```python
AudioSegment(id, start_ms, end_ms, pcm, sample_rate)
TranscriptEvent(id, segment_id, start_ms, end_ms, text, is_final)
ConversationSnapshot(revision, committed_history, pending_utterances)
GenerationJob(id, revision, snapshot, status, cancel_token)
TTSChunk(job_id, revision, chunk_index, text, audio)
```
- Use monotonic timestamps and stable IDs; deduplicate overlapping STT windows.
- Maintain a bounded audio ring buffer (start at 30 s) and a durable/in-memory transcript event log; persist log if recovery across restart is required.
- Never discard uncommitted utterances on cancellation, failure or timeout.
- Apply backpressure: bounded queues, explicit overflow metrics, and configurable retention; if RAM pressure occurs, spill audio to disk or signal degraded mode rather than silently dropping speech.

## 5. Turn management / cancellation
1. VAD detects speech; capture continues regardless of Qwen state. If TTS is playing, immediately stop playback on *confirmed* barge-in; use echo suppression / AEC if microphone hears speakers.
2. STT emits final transcript. Append event, increment `revision`, and update `pending_utterances`.
3. If a Qwen job exists, mark its revision **stale** and request cooperative cancellation. Wait for worker acknowledgment before starting the replacement job; do not run two Qwen generations.
4. Debounce short consecutive utterances (initial 300–600 ms, configurable), then build one snapshot from committed history plus **all** pending utterances. Do not enqueue one Qwen job per STT fragment.
5. For every generated token, sentence, synthesized chunk and playback action, check `revision == current_revision`. Drop stale output. Commit assistant answer and mark pending utterances processed **only after** the generation is accepted as current; never commit cancelled answers.
6. New speech may be an addition, correction or separate request. Default v1 behavior: include all pending utterances chronologically in the next snapshot; do not silently replace earlier user speech. Semantic coalescing is optional, not required for correctness.
7. If user continuously speaks, use a max wait window (e.g. 1.5 s after first final transcript) to avoid indefinite debounce; subsequent speech may invalidate the in-flight answer.

## 6. Streaming and latency
- Qwen emits tokens incrementally; buffer until safe Vietnamese sentence/clause boundary, then enqueue TTS with `revision` and `chunk_index`.
- Playback follows chunk order. Never play a chunk from an old revision; stop active playback and flush pending chunks on barge-in.
- Avoid speaking unfinished or speculative content. Track time to first token (TTFT), time to first audible audio (TTFA), and end-to-end latency.
- When Qwen is slower than incoming speech, buffering prevents loss **but cannot guarantee immediate answers**. Bound pending inference jobs to **one replaceable latest snapshot**, while retaining the full transcript log.

## 7. Modules to implement
```text
src/audio_capture.py      # ALSA/GStreamer capture, ring buffer
src/vad.py                # CPU VAD + segmentation
src/stt_worker.py         # Whisper adapter, final transcript events
src/conversation.py       # event log, revisions, snapshots, commit
src/slm_worker.py         # Qwen streaming + cooperative cancel
src/tts_worker.py         # Vietnamese TTS adapter + chunk ordering
src/playback.py           # interruption + audio output
src/orchestrator.py       # async queues, task lifecycle, backpressure
src/metrics.py           # JSONL telemetry
config.yaml               # all tunable thresholds / paths
benchmarks/               # isolated and integrated benchmarks
 tests/                   # unit/integration tests
```
- Define adapters so the existing STT/TTS runtimes can be retained; do not rewrite working inference code without need.
- Add startup compatibility checks for JetPack/CUDA/model formats; log versions, memory, power mode and chosen inference devices.

## 8. Benchmark plan
- Record >=30 warm runs per case with same power mode, clocks and temperature envelope; report median/p95 and RAM/GPU metrics.
- A: STT alone — latency, real-time factor, word error checks.
- B: Qwen alone — prompt prefill, TTFT, tokens/sec, total latency, output token count; compare current backend against `llama.cpp` Q4.
- C: TTS alone — first-audio latency and real-time factor.
- D: full pipeline — TTFA, end-to-end latency, RAM peak, GPU load, temperature, power throttling.
- E: user interrupts while Qwen is running and while TTS is playing — cancellation latency, stale audio count, dropped utterance count, oldest pending age.
- F: concurrent versus serialized GPU inference; compare CPU-STT option if viable.
- Collect `tegrastats --interval 500`, process timings, and model-level token/segment counts. Optimize **TTFA and correctness**, not just aggregate GPU utilization.

## 9. Acceptance criteria
- No uncommitted final transcript is lost during Qwen generation/cancellation.
- Exactly one Qwen generation is active at any instant.
- No stale-revision TTS audio starts playing; active playback stops after confirmed barge-in (target <200 ms, validate on hardware).
- Audio capture continues during STT/SLM/TTS work; queues remain bounded and overflow is observable.
- A correction such as 'Hà Nội hôm nay?' followed by 'Ý tôi là ngày mai' produces a response using the correction, not the outdated prompt.
- After failure or cancellation, pending utterances remain recoverable and are processed in the next valid snapshot.
- Provide benchmark results and document whether concurrent GPU execution improves or worsens TTFA.

## 10. Delivery instructions for coding agent
1. Inspect the existing repository, model loading paths, JetPack/CUDA versions, and measured latency before modifying code.
2. Implement the smallest end-to-end v1 with existing model adapters and the revision/cancellation mechanism.
3. Add automated tests for interruptions, concurrent transcript arrivals, queue overflow, worker crashes, and stale TTS suppression.
4. Benchmark on Xavier, then tune chunk sizes, quantization, CPU/GPU placement and cancellation cadence based on measurements.
5. Deliver runnable commands, config defaults, test output and a concise benchmark comparison. Do not claim real-time performance without on-device measurements.
