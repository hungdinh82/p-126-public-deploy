# Evaluation Evidence — VIVI Cabin Copilot

> **Nhóm:** The Odysseans (P-126) · **Mã đề:** DEV-01
> **Mục đích:** Tổng hợp số liệu đánh giá hiệu quả cho Demo Day — gộp từ các bộ benchmark trong [`eval/`](../eval/README.md), [`benchmarks/agent/`](../benchmarks/agent/README.md) và deep-dive SLM local trong [JOURNAL.md](../JOURNAL.md).

## 1. Headline numbers

| Hạng mục | Kết quả | Nguồn |
|----------|---------|-------|
| **Core workflow (orchestrator + domain + SQLite + receipts)** | **30/30 episodes passed** | [`eval/results/core-v2.json`](../eval/results/core-v2.json) |
| **Backend latency P/D (bình thường)** | p50 **75 ms** · p95 **86 ms** (200 samples, budget 300 ms) | [`eval/results/controls-v2.json`](../eval/results/controls-v2.json) |
| **Backend latency P/D (model/map pending)** | p50 60-75 ms · p95 67-82 ms (budget 500 ms) | ↑ |
| **Semantic holdout — Google gemini-3.5-flash-lite** | **7/8 cases** (87,5%) | [`eval/results/semantic-holdout-v2.json`](../eval/results/semantic-holdout-v2.json) |
| **Semantic dependencies** | **5/6 cases** (83,3%) | [`eval/results/semantic-dependencies-v2.json`](../eval/results/semantic-dependencies-v2.json) |
| **SLM local Qwen3-1.7B — pipeline schema strict** | **0/10 served** (5 ReadTimeout + 5 ValidationError) | [JOURNAL deep-dive](../JOURNAL.md#deep-dive-2026-10-18--giới-hạn-slm-cục-bộ-qwen3-17b-trên-jetson-agx-xavier) |
| **SLM local — pipeline schema nới lỏng** | 7/10 served, **2/7 đúng route** | ↑ |
| **OpenRouter Qwen3.7 Flash (57 câu dev)** | **77,2% route accuracy**, p50 6,0 s, p95 12,6 s | ↑ |
| **SLM thô — TTFT** | **145 ms** (46,7 token/giây) | ↑ |

**Đọc nhanh:**
- Backend + orchestrator đang **chắc chắn**: 100% workflow pass, p95 dưới budget có biên rộng.
- Semantic live với Google đang **đủ dùng** ở phạm vi dev (>83%); vẫn còn 2 ca trượt đã có root cause.
- SLM local **chưa đạt chất lượng pipeline** vì schema + prompt quá nặng với 1.7B — đã kiểm chứng bằng benchmark, không phải "model quá nhỏ không chạy".

---

## 2. Bộ benchmark & dataset

| Bộ | Dependency | Đo gì | Dataset |
|---|---|---|---|
| `workflow_benchmark` (core-v2) | LangGraph, SQLite, orchestrator **thật**; map/xe tổng hợp | Task, consent, slot, draft/journey, fallback, replay/collision, stream, restart | [`eval/core_cases.jsonl`](../eval/core_cases.jsonl) — 30 episodes |
| `navigation_controls_benchmark` (controls-v2) | Journey đã confirm; model/map bị giữ bằng barrier | Backend P/D ở 3 chế độ: bình thường, model chờ, map chờ | Fixture SHA256 `70dc0181…` — 600 samples (100/gear × 3 load × 2 gear) |
| `semantic_benchmark` (semantic-holdout-v2, dependencies, after-fix) | Model LIVE do người chạy chọn (Google gemini-3.5-flash-lite); không có map/xe | Capability + slot trên dataset ngôn ngữ riêng | 3 bộ: 8 + 6 + 6 cases |
| `benchmark` (RAG) | Cẩm nang + embedding/model local tùy chọn | Retrieval/answer development benchmark | [`eval/local_tool_regression_cases.jsonl`](../eval/local_tool_regression_cases.jsonl) 28 ca |
| `agent benchmark` (feature-based) | SLM local + fixtures | Failure reports theo feature | [`benchmarks/agent/`](../benchmarks/) |

**Nguyên tắc dataset:**
- Mỗi operation có ID, input/semantic, oracle `expect` **độc lập** — không gọi parser để tạo stub.
- Semantic dataset được viết trong quá trình phát triển; **chưa phải held-out đã duyệt** — không được công bố là độ chính xác tổng quát.
- Report mặc định ghi: `created_at`, `revision`, `working_tree_dirty`, `environment`, `dataset_sha256`, `limitations`.
- Runner offline dùng DB tạm, chặn mạng ngoài loopback, không đọc/ghi DB người dùng.

---

## 3. Backend latency — `controls-v2.json`

> Đo tại revision `5a549456` · 2026-10-15 · Linux 6.18 WSL2, Python 3.12.12 · 200 samples/run, 100/gear, batch 10.

| Load | Budget | Backend p50 | Backend p95 | P (p50 / p95) | D (p50 / p95) | Passed |
|------|-------:|------------:|------------:|--------------:|--------------:|:------:|
| **normal** (không ai chờ) | 300 ms | 75,08 ms | 86,10 ms | 75,08 / 84,83 | 75,06 / 91,67 | ✅ |
| **model_pending** | 500 ms | 75,35 ms | 82,52 ms | 75,28 / 82,21 | 75,35 / 82,52 | ✅ |
| **map_pending** | 500 ms | 60,18 ms | 67,52 ms | 59,75 / 67,36 | 60,55 / 67,52 | ✅ |

**Diễn giải cho BGK:**
- Backend P→D (chuyển số) phản hồi **~75 ms** ở điều kiện bình thường — nhanh hơn budget 300 ms khoảng **4×**.
- Khi model hoặc map đang chờ, backend vẫn giữ dưới 100 ms (budget 500 ms) — chứng minh orchestrator không bị chặn bởi dependency chậm.
- Phạm vi: **orchestrator + navigation domain + SQLite + durable receipt**. Chưa gồm HTTP/browser, mạng xe, phần cứng Jetson.

---

## 4. Core workflow — `core-v2.json`

> Profile `authored_semantic_contract` · dataset SHA256 `611b3998…` · 30 episodes.

| Nhóm | Đo gì |
|------|-------|
| `journey_lifecycle` | Navigate-then-music, draft → preview → consent → start → arrival |
| `consent_safety` | Cửa/kính yêu cầu "Xác nhận/Hủy"; timeout 30s; không reuse ack cũ |
| `slot_resolution` | Slot thiếu → agent hỏi; slot ambiguous → clarify |
| `fallback` | Google timeout → local → rules |
| `replay_collision` | Idempotency, không double-execute khi retry |
| `restart` | State persist qua SQLite checkpoint |

**Kết quả: 30/30 passed.** Mọi episode đi qua đầy đủ các checkpoint `exists / expected / actual` cho:
- `navigation_workspace.draft.route`
- `pending_question.purpose` = `approve_execution`
- `observed.vehicle.driving` / `.media_playing`
- `diagnostics.semantic_source` = `llm`

Giới hạn (nguyên văn từ report):
- *"Fake models, maps and vehicle; does not measure semantic accuracy or live adapters."*
- *"Authored cases used during development; not a held-out language evaluation."*

---

## 5. Semantic live — Google gemini-3.5-flash-lite

Ba bộ live đã chạy, giữ nguyên điểm và prompt/hash tại thời điểm đo. **Không gộp điểm giữa các phiên bản prompt** hoặc tính lại bộ đã dùng sửa như held-out.

| Bộ | Cases | Passed | % | Fallbacks | Failed IDs | Giai đoạn |
|----|------:|-------:|--:|----------:|-----------|-----------|
| `semantic-holdout-v2` | 8 | **7** | **87,5%** | 0 | `holdout-itinerary` | Trước explicit purpose safeguard |
| `semantic-holdout-after-fix-v2` | 6 | **4** | **66,7%** | 0 | `purpose-new-2`, `purpose-new-3` | Trước clarifying dependent-action inclusion |
| `semantic-dependencies-v2` | 6 | **5** | **83,3%** | 0 | `deps-new-5` (nhầm purpose) | Đo cuối |

**Before/after fix (cùng phương pháp đo):**

| Metric | holdout v1 (8 cases) | holdout after-fix (6 cases) | dependencies (6 cases) |
|--------|---------------------:|----------------------------:|-----------------------:|
| Pass rate | 7/8 (87,5%) | 4/6 (66,7%) | 5/6 (83,3%) |
| Fallbacks | 0 | 0 | 0 |

> **Diễn giải trung thực:** sau khi phát hiện lỗi ở `holdout-itinerary` → sửa semantic instruction → chạy lại dataset mới `after-fix` → vẫn còn 2 ca trượt (`purpose-new-2`, `purpose-new-3`) do chưa clarify dependent-action inclusion. Sau đó clarify tiếp → bộ `dependencies` đạt 5/6, còn `deps-new-5` nhầm purpose.
>
> Đây là **quy trình phát triển có đo lường**, không phải "ngẫu nhiên pass". Mỗi vòng đều có `semantic_instruction_sha256` để trace prompt nào đạt điểm nào.

---

## 6. SLM local trên Jetson AGX Xavier — Qwen3-1.7B Q4_K_M

> Chi tiết đầy đủ: [JOURNAL deep-dive 2026-10-18](../JOURNAL.md#deep-dive-2026-10-18--giới-hạn-slm-cục-bộ-qwen3-17b-trên-jetson-agx-xavier)

| Hạng mục | Kết quả |
|----------|---------|
| **SLM thô — TTFT p50** | 145 ms |
| **SLM thô — tổng ≤96 token** | 455 ms |
| **Tốc độ sinh** | 46,7 token/giây |
| **Pipeline schema strict** | **0/10 lượt** (5 ReadTimeout + 5 ValidationError) |
| Latency strict (cả lượt) | p50 **35,8 s** · p95 **48,3 s** |
| **Pipeline schema nới lỏng** | **7/10 lượt** served, 2/7 đúng route |
| Latency model (nới lỏng) | p50 **9,1 s** |
| **OpenRouter Qwen3.7 Flash** | **77,2%** route accuracy (57 câu dev) |
| OpenRouter latency | p50 6,0 s · p95 12,6 s |

**Resource contention khi STT + SLM + TTS chạy đồng thời:**

| Metric | Riêng lẻ | Đồng thời | Thay đổi |
|--------|---------:|----------:|---------:|
| LLM TTFT | 145 ms | 286 ms | **+97%** |
| Tốc độ sinh | 46,7 tok/s | 27,9 tok/s | **-40%** |
| TTS TTFA p95 | 125 ms | 757 ms | **+505%** |
| TTS RTF p95 | 1,3 | 2,6 | **×2** |
| GPU peak | - | **99%** | - |

**Kết luận (có bằng chứng):**
- Model nhanh khi prompt ngắn → điểm nghẽn **không phải GPU hay model size** mà là **contract pipeline quá lớn** (~8.000 token system prompt, ~550 token output với schema strict).
- Nới schema giảm completion 554 → 80 token → model phục vụ được 7/10 lượt, nhưng accuracy chỉ 2/7 → lộ **giới hạn hiểu nghiệp vụ của 1.7B tổng quát** (không fine-tune theo miền).
- Giả định "cần model ≥20B" chưa chứng minh bằng benchmark. Jetson 14,9 GB bộ nhớ dùng chung đã dùng tối đa 9,27 GB — model 20B quantized có thể làm **latency tệ hơn**.

---

## 7. Benchmark so sánh — Google vs OpenRouter vs Local

| Provider | Model | Dataset | Pass rate | Latency p50 | Latency p95 |
|----------|-------|---------|-----------|-------------|-------------|
| **Google** | gemini-3.5-flash-lite | semantic-holdout v1 (8) | 7/8 = **87,5%** | n/a | n/a |
| **Google** | gemini-3.5-flash-lite | semantic-dependencies (6) | 5/6 = **83,3%** | n/a | n/a |
| **OpenRouter** | Qwen3.7 Flash | 57 câu dev | 77,2% | **6,0 s** | **12,6 s** |
| **Local SLM** | Qwen3-1.7B Q4_K_M (Jetson) | 10 câu, strict schema | **0%** | timeout | timeout |
| **Local SLM** | Qwen3-1.7B Q4_K_M (Jetson) | 10 câu, nới schema | 2/7 served đúng | 9,1 s | n/a |

> **Baseline so sánh:** Google là tầng mặc định đạt chất lượng cao nhất. OpenRouter là backup cloud khi mất Google. Local là fallback khi mất Internet — hiện chưa đạt chất lượng semantic nhưng vẫn giữ được chức năng cabin cơ bản nhờ fast-path deterministic (xem `src/vivi/gateway/`).

---

## 8. Hạn chế của kết quả hiện tại

Trung thực nêu ra để BGK đọc đúng phạm vi:

- Bộ OpenRouter: **57 câu dev**, nhãn ở trạng thái **draft**.
- Bộ local: chỉ **10 câu**.
- **Chưa có tập held-out độc lập** — mọi dataset hiện tại đã được dùng trong quá trình phát triển.
- Audio thật chưa được gán nhãn thủ công → **WER/CER chưa dùng làm số liệu chính thức**.
- Chưa benchmark model local đã fine-tune theo miền (SFT/LoRA).
- Chưa đo E2E hoàn chỉnh (end-of-speech → first TTS byte) trên pipeline local **ổn định**.
- Semantic benchmark chỉ đo **interpreter**; chưa đánh giá map, vehicle, speech hay response-generation.
- `core-v2` dùng fake models/maps/vehicle — không đo semantic accuracy hay live adapters.
- Tất cả report có `working_tree_dirty: true` — cần chạy lại trên commit sạch cho phiên bản release.

**Kết luận hạn chế:** Số liệu đủ để **xác định điểm nghẽn + định hướng kỹ thuật**, chưa đủ để tuyên bố ngưỡng model tối thiểu hay accuracy production.

---

## 9. Reproducibility

Chạy lại các benchmark trên:

```bash
# Core workflow (fake models/maps/vehicle) — 30 episodes
.venv/bin/python -m eval.workflow_benchmark --repeats 3

# Backend latency P/D — 600 samples
.venv/bin/python -m eval.navigation_controls_benchmark --samples 100

# Semantic live với Google (chủ động gọi model thật)
.venv/bin/python -m eval.semantic_benchmark --provider google

# Semantic live với Local trên Jetson
.venv/bin/python -m eval.semantic_benchmark --provider local \
  --contract operations_v1 --transport json_schema \
  --dataset eval/local_tool_regression_cases.jsonl \
  --output /tmp/local-tools.json

# RAG retrieval/answer benchmark
.venv/bin/python -m eval.benchmark
```

Mỗi report ghi `revision` và `dataset_sha256`, trace được về đúng phiên bản dataset và code đã chạy.

---

## 10. Liên kết

- [`eval/README.md`](../eval/README.md) — chi tiết dataset và nguyên tắc đo
- [`eval/results/`](../eval/results/) — JSON report gốc (có timestamp, revision, SHA256)
- [`docs/core_v2_acceptance.md`](core_v2_acceptance.md) — evidence và giới hạn nghiệm thu
- [`docs/local_tool_pipeline.md`](local_tool_pipeline.md) — luồng SLM local, catalog, workflow test
- [`docs/investigations/local-model-runtime-2026-10-17.md`](investigations/local-model-runtime-2026-10-17.md) — Jetson runtime investigation
- [`JOURNAL.md`](../JOURNAL.md) — nhật ký phát triển + deep-dive SLM
- [`benchmarks/agent/README.md`](../benchmarks/agent/README.md) — feature-based agent benchmark
