> Báo cáo lịch sử của nhánh main ngày 15/10/2026. Các phần local forms, voice inbox và budget cũ không thuộc runtime v2 hiện tại. Xem [tích hợp hiện tại](JETSON_MAIN_INTEGRATION.md); số đo dưới đây không phải nghiệm thu bản merge.

# Jetson voice pipeline benchmark

Device: Jetson AGX Xavier 16 GB, JetPack/L4T R35, CUDA 11.8, compute capability 7.2. The candidate ran from `feat/jetson-realtime-voice-pipeline` on staging ports 8795/1249/1250, with model artifacts read from the existing data directory and logs isolated under `/mnt/jetson-data/vivi-ai-staging`.

Behavior update, 2026-10-15: the added offline-grammar overrides were removed
after they rejected valid SLM interpretations when a phrase was outside the
grammar. The measurements below are historical: cases reported as repaired or
rejected by those overrides do not describe the current model-output policy.
The original main-branch guards, schema/domain validation, current-query
grounding and asynchronous speech buffering remain. Offline snapshot parsing
is still available as a fallback, rather than replacing a successful SLM plan.

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

The initial pipeline figures above did not measure the real planner prompt. The direct staging measurements below use the actual system instructions and capability schemas generated in memory by the application, without committing a rendered prompt fixture.

## Direct staging investigation: STT context isolation

The deployed native Whisper server defaulted to `no_context=false`. Its resident
decoder keeps `state->prompt_past` between inference calls; `no_context=true`
clears that token history at the start of each call. The gateway did not send the
option and the launcher did not enable it. Both now explicitly enable
`no_context`, without changing `ggml-base.bin` or the inference backend.

Six recent real inbox records contained the repeated transcript “Tôi like em
quá” or “Tôi like lắm”. Their context hashes were identical, while their audio
fingerprints differed. They therefore came from different audio payloads, rather
than replaying the same uploaded recording. Audio had already been removed under
the normal retention policy, so this does not establish the recognition error
rate or prove that retained decoder context caused those specific errors.

A direct comparison used ZeroTTS INT8 CPU to generate two Vietnamese utterances,
converted to mono PCM16 16 kHz. Each comparison decoded the same sequence:
“Tôi lạnh quá”, “Mở cửa kính bên tài”, then the first recording again. The decoder
was reset before each comparison. These are synthetic audio checks, not a
microphone accuracy benchmark.

| Decode mode | First / second / repeated STT wall time |
|---|---:|
| Previous context enabled | 325.5 / 444.0 / 331.6 ms |
| Independent request context | 288.0 / 463.7 / 319.8 ms |
| Updated gateway, independent context | 323.6 / 461.0 / 336.8 ms |

Both modes transcribed the first recording as “Tôi lại quá”. The gateway returned
different text for the second recording (“Mở cực kính bên tài.”), then the first
text for the repeated audio. Context isolation removes cross-request influence;
it does not fix the existing model's Vietnamese recognition errors. A multipart
regression test verifies that every request explicitly sends `no_context=true`
and forwards its own audio bytes. The focused pipeline test set passed 23 tests.

## Direct staging investigation: current planner candidate

MAXN mode was confirmed, with GPU reaching 1,377 MHz during inference. The same
Qwen3-1.7B Q4_K_M model retained 29 GPU layers. The candidate launcher disables
llama.cpp's archived host-RAM prompt cache (`--cache-ram 0`); active GPU KV reuse
remains enabled. RAM available after reload was approximately 7.8 GiB before the
speech tests, compared with approximately 1.9 GiB during the earlier cache buildup.

Actual resolver requests included the current local system instructions and
capability-bound native tool schema, capped at 512 output tokens. Four probes
used 8,056–8,074 prompt tokens and 45–83 completion tokens. The first request after
restart took 14.8 s; the next three took 2.36, 2.37 and 3.75 s. The latter requests
reused 8,040 prefix tokens. These timings are exploratory measurements, not the
required 30-run benchmark or a production acceptance result.

At this intermediate checkpoint, correctness still failed: the raw window proposal selected the wrong seat (the
literal-command guard corrected it); the merged correction emitted both 24 and
26 degrees and omitted the window request; an unclear command produced unrelated
vehicle actions. Those intermediate results were rejected. The final chronological composition and
grounding changes below address the observed failures; timing alone was not an
acceptance criterion.


## Validated revision and grounding changes

The conversation owner now composes newline-separated pending speech in order.
A contextual correction updates the most recent compatible draft, using the
registry's argument/exclusion merge rules. Repeated consecutive speech does not
repeat effects. Unrecognized control fragments block the plan and retain known
drafts for clarification. All original transcripts remain in the inbox/history.
This happens at the semantic boundary on the laptop, not on the Jetson server.

Completed tasks are excluded from active model context. The current input follows
bounded history in both planner and answer-generation prompts. Conversation
queries cannot be substituted with a model-selected old history query. Literal
controls constrain capability, named target and numeric meaning for every
provider; models never authorize unrelated effects from an unclear command.

Local planner output is capped at the configured local limit (512 tokens here),
rather than being overwritten with the global 2,048-token planner limit. Native
tools omit unused fields and bind each capability to its allowed argument slots.
The archived host-RAM prompt cache is disabled by default; the loaded model and
active GPU KV cache remain unchanged.

A replaced generation waits for HTTP/upstream cleanup before the next snapshot.
This also covers queued jobs and disconnects before the first streaming token.
Native logs confirmed `cancel task` and slot release. Predispatch failures retry
with backoff while preserving pending text; dispatched effects remain bound to
the execution ledger for reconciliation. The browser fences queued speech during
recording upload and STT, before a new server revision is available.

### Warm planner measurements

Thirty measured warm runs per case, with one case-specific warmup excluded:

| Input case | Median | p95 | Grounded/challenged proposals |
|---|---:|---:|---:|
| Set temperature | 1,455.76 ms | 1,462.65 ms | 0/30 |
| Open driver window | 2,302.16 ms | 2,320.47 ms | 30/30 |
| 24 degrees → correct to 26 → add passenger window | 2,623.17 ms | 2,636.27 ms | 30/30 |
| Repeat an unclear control | 2,384.72 ms | 2,424.62 ms | 30/30 |
| Cold complaint after an unrelated completed topic | 1,543.82 ms | 1,554.88 ms | 30/30 |

All 150 final semantic outputs passed the case assertions. These results do not
claim Qwen's raw proposals are reliable: the grounding/composition layer was
needed in four cases. The earlier strict full-schema probe used 10,465 prompt
and 595 output tokens, with approximately 17.4 s on two warm runs and invalid
semantic output. The compact temperature case used 8,058 input and 45 output
tokens. The baseline is exploratory, not a matched 30-run statistical comparison.
The first compact request after restart took 14.8 s; warm figures exclude that
prefill. This is not a guarantee of two-second answers for arbitrary input.

### Speech, contention and placement

STT CPU and CUDA used the same saved PCM16 WAV; its hash is in the JSON report.
Thirty CUDA runs per case, excluding one warmup:

| Case | Median | p95 |
|---|---:|---:|
| STT alone | 232.36 ms | 279.16 ms |
| STT while Qwen generates | 434.19 ms | 446.15 ms |
| TTS INT8 CPU stream, complete sentence | 2,463.73 ms | 2,722.80 ms |

Median TTS first audio was 315.08 ms. Qwen stayed active during all 30 concurrent
speech checks; each STT request completed without a queue rejection. Median
cancellation acknowledgment is recorded in the JSON report (approximately 50–60 ms).
Separate nonstreaming/streaming probes released the gateway in 62.1/48.9 ms;
short replacement requests completed in 436.5/397.3 ms. Native logs confirmed
that cancellation stopped upstream Qwen, not just the waiting HTTP client.

Whisper CPU was rejected after seven measured warm requests: median 6,748.69 ms,
versus 232.36 ms for CUDA on the same audio. The CPU exploration was stopped
rather than spending 30 rounds on the clearly slower option; no CPU p95 or
30-run comparison is claimed. CUDA remains the default; `JETSON_AI_STT_DEVICE=cpu`
is an explicit experiment, independent of the Qwen/TTS device selection.

The earlier 7–10 s STT cancellation probes used silence and produced hallucinated
text. They must not be treated as proof that ordinary spoken audio stalls on the
GPU. Whisper-base still misrecognizes some synthetic Vietnamese words, even with
fresh decoder context; this work does not claim a microphone word-error rate or
eliminate the model's recognition limitations.

### Integration and resource checks

A separate laptop API/runtime, isolated under `/tmp`, called the real Jetson.
While the first Qwen request was active, two more final transcripts arrived.
All three completed with response revision 3 and one generation response. The
accepted plan set 26 degrees once and retained the passenger window's normal
confirmation requirement. The combined reply completed 10.24 s after the added
speech in that cold/cancellation run. No user workflow/history database was used
for those synthetic turns.

Playwright entered the driver UI and clicked the microphone using a fake capture
device. Recording started without page errors or the previous AudioContext
ReferenceError. Browser regression tests also verify that old speech remains
queued during upload/STT and is discarded after a newer revision is recognized.
This does not measure physical speaker-stop latency or acoustic echo rejection.

The telemetry capture contained 829 samples: RAM used peaked at 8,475 MB and GPU
temperature at 79.5°C. Available memory remained above approximately 6 GiB during
the measured planner/speech rounds; no growing host prompt-cache archive was
observed. MAXN and all 29 Qwen CUDA layers were preserved. The server remains
stateless inference only; buffering, transcript storage and task ownership stay
on the laptop.

Reproduce the focused measurements from the laptop checkout:

```bash
PYTHONPATH=. python scripts/benchmark_jetson_planner.py --rounds 30
```

Run speech measurements on Jetson, after capabilities report all models ready:

```bash
python scripts/benchmark_jetson_speech.py --rounds 30
```

The commands use the configured staging API at port 8795 by default. The normal
service command is still `bash scripts/jetson_ai.sh run --tts-precision int8`
with the staging environment supplied by the operator. Omitting the TTS precision
option continues to select fp32; the INT8 CPU profile here is the explicitly
requested staging test configuration.
