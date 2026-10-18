# Development Journal — The Odysseans (Nhóm P-126)

> Nhật ký phát triển VIVI Cabin Copilot (Mã đề DEV-01). Mỗi tuần ghi lại: học được gì, khó khăn gì, quyết định gì, kế hoạch tiếp.

## Thành viên

| Vai trò | Họ tên | MSSV | GitHub |
|---------|--------|------|--------|
| 🧭 Trưởng nhóm | Đinh Văn Hùng | 2A202602443 | @hungdinh82 |
| Thành viên | Nguyễn Thanh Phong | 2A202602843 | @kaisye |
| Thành viên | Nguyễn Minh Dương | 2A202602920 | @minhduong814 |
| Thành viên | Hoàng Trung Khải | 2A202602947 | @khaihoang004 |

Tham chiếu công việc chi tiết theo ngày: [WORKLOG.md](WORKLOG.md).

---

## Week 1: 2026-09-15 → 2026-09-21 — Khởi động & kiến trúc

### Mục tiêu tuần này
- [x] Lập repository, chốt scaffold FastAPI + LangGraph
- [x] Chọn stack voice: PhoWhisper (STT) + ZeroTTS (PC) + adapter trên Nano
- [x] Vẽ bản đồ kiến trúc edge-first (xem [docs/architecture_diagram.md](docs/architecture_diagram.md))

### Đã hoàn thành
- Scaffold `src/vivi/` với FastAPI app, LangGraph graph tối thiểu
- Chọn SQLite FTS5 làm handbook offline (xem [docs/handbook_sqlite.md](docs/handbook_sqlite.md))
- Chốt chạy edge-first: toàn bộ voice → agent → vehicle control phải xong offline

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Jetson Nano 4 GB RAM không đủ cho LLM nguyên bản | Quyết định lượng tử hóa SLM + chạy TTS INT8 | Set baseline latency < 2.5s |
| Hai entrypoint song song (`/assist` cũ và `src.vivi.api.app`) gây rối | Chuẩn hóa runtime về `src.vivi.api.app:app` | Contract Core v2 (xem [docs/langgraph_core_v2.md](docs/langgraph_core_v2.md)) |

### Bài học
- Edge-first không phải "chạy local khi mất mạng" — là "mọi tác vụ cabin phải chạy ngay cả khi WAN chết hẳn".
- Mỗi quyết định tech phải hỏi: "chạy được trên Nano 4 GB không?" trước khi hỏi "chất lượng ra sao?".

### Kế hoạch tuần sau
- [ ] Chốt contract LangGraph (state, nodes, reducer)
- [ ] Build handbook ingestion → SQLite FTS5
- [ ] Vehicle simulator (digital twin) MVP

---

## Week 2: 2026-09-22 → 2026-09-28 — Contract LangGraph & handbook RAG

### Mục tiêu tuần này
- [x] Chốt state schema và conditional edges cho LangGraph graph
- [x] Vehicle simulator chạy được in-memory
- [x] Handbook SQLite tra được bằng FTS5

### Đã hoàn thành
- `vehicle_simulator/` — digital twin trong bộ nhớ + policy demo
- Conversation generalization: slot địa điểm, phân giải POI dùng chung (xem [docs/conversation_generalization_runtime.md](docs/conversation_generalization_runtime.md))
- Golden dataset v1 từ phỏng vấn 5 user mô phỏng (xem `eval/golden_dataset.jsonl`)

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Model local hay bịa tên dòng xe | Grounding bắt buộc: mọi câu trả lời handbook phải cite trích đoạn SQLite | Hallucination 0% trên 30 câu on-topic |
| Agent gọi nhầm tool "mở cửa" khi user chỉ nói "mở nhạc" | Thêm topic guardrail + Pydantic schema-lock | Jailbreak test rớt dưới 5% |

### Bài học
- Guardrail prompt một mình **không đủ** — phải có code-level validator (Pydantic + banned-pattern) song song.
- Golden dataset nhỏ (30 câu) nhưng từ user thật mạnh hơn 300 câu bịa.

### Kế hoạch tuần sau
- [ ] Streaming SLM output
- [ ] Dashboard kỹ sư để quan sát latency từng stage

---

## Week 3: 2026-09-29 → 2026-10-05 — Streaming & engineer dashboard

### Mục tiêu tuần này
- [x] SLM streaming + STT/TTS streaming
- [x] Engineer dashboard hiển thị flow call real-time
- [x] CI pipeline chạy pytest + ruff tự động

### Đã hoàn thành
- `monitor.html` + `monitor.js` — dashboard quan sát trip/latency
- Scenario Lab chạy on memory runtime
- Pytest harness: `tests/unit/`, `tests/integration/`, `tests/contract/`

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Streaming làm UI chat render reasoning token thành bong bóng rời | Phân biệt stream reasoning vs stream reply tại gateway, UI chỉ render reply | UI sạch |
| Pytest flaky vì agent gọi Google khi test | Tất cả test agent dùng graph double, cô lập khỏi AI gateway | CI xanh ổn định |

### Bài học
- Rò rỉ reasoning ra UI là lỗi UX nghiêm trọng — phải chặn ở contract chứ không bằng CSS.
- Test isolation (graph double) khó setup lúc đầu nhưng cứu cả tuần sau mỗi lần refactor.

### Kế hoạch tuần sau
- [ ] Internet search có guardrail
- [ ] Benchmark voice trên Jetson AGX Xavier

---

## Week 4: 2026-10-06 → 2026-10-12 — Online/offline adaptive + navigation

### Mục tiêu tuần này
- [x] Adaptive gateway: Google first → local fallback → parser
- [x] Internet search qua Tavily API, có guardrail
- [x] Navigation + charge planning MVP

### Đã hoàn thành
- `src/vivi/gateway/` — adaptive online/offline AI gateway (Hùng)
- Tavily internet search với topic guardrails (Dương)
- Navigation node + charge planning (Dương, PR #28)
- Wake-word dataset + training + follow-up listening (Phong, PR #29/#30)
- Model streaming SLM/STT/TTS (Khải, PR #25/#27)

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| Gateway không biết Google đã timeout hay thật sự mất mạng | Thêm circuit breaker đo 3 lần fail liên tiếp mới fallback local | Latency ổn hơn khi mạng chập chờn |
| Wake-word hay false-positive với tiếng ồn cabin | Train lại trên noisy dataset, thêm follow-up confirmation | FPR giảm còn ~2% |
| Pytest fail trên macOS vì `ps` không có `/proc` | Phát hiện và fix cross-platform RSS reader | Dev team macOS chạy được local |

### Bài học
- "Online-first với fallback" dễ hơn "offline-first với upgrade" **về code** nhưng tệ hơn **về UX khi mạng chập chờn**. Chọn offline-first là đúng.
- Mỗi feature mới (navigation, search, wake-word) đều phải có fixture test riêng, không chỉ smoke test.

### Kế hoạch tuần sau
- [ ] Unified gateway refactor
- [ ] JSON grammar để simplify SLM context
- [ ] Benchmark local vs Google model

---

## Week 5: 2026-10-13 → 2026-10-18 — Unified gateway, benchmark & demo prep

### Mục tiêu tuần này
- [x] Unified LLM gateway (bỏ nhiều code đường vòng)
- [x] JSON grammar cho SLM
- [x] So sánh model local vs Google trên golden dataset
- [x] Form-owned model: SLM tự submit form validated
- [x] Pitch deck 18 slides + Live URL public

### Đã hoàn thành
- Unified gateway (Dương refactor, PR #40)
- Form-owned request model với validation (Khải)
- Benchmark output trong `eval/results/`
- Live URL: https://p126-vivi.onrender.com/
- `presentation/pitch_deck.html` + `pitch_deck.pdf`

### Khó khăn & Giải pháp
| Khó khăn | Giải pháp | Kết quả |
|----------|-----------|---------|
| SLM local nhiều lần đưa ra tool proposal không ground với state | Thêm validator "separate tool proposals" trước khi execute | Tỉ lệ gọi tool sai giảm mạnh |
| TTS INT8 trên Xavier 8.5 build không support TRT 8.6 API | Guard TensorRT 8.6 APIs trên Xavier 8.5 builds | TTS chạy được cross-platform |
| Deploy Render free tier sleep sau 15 phút không hoạt động | Dùng UptimeRobot ping /health mỗi 5 phút | Live URL stable |

### Bài học
- Refactor "unified gateway" nên làm sớm hơn — 3 tuần đầu có 2 code path song song gây bug lặp.
- Benchmark local vs cloud chỉ có ý nghĩa khi dùng cùng golden dataset — trước đó compare bằng cảm giác.

### Kế hoạch tuần sau (Demo Day)
- [ ] Quay video demo 3-5 phút theo AIDA
- [ ] Seed showcase mode với dummy data đẹp
- [ ] Pre-login demo account
- [ ] Tổng duyệt 3 lần + Q&A prep
- [ ] Backup 3 tầng (live → video → screenshot)

---

## Deep-dive 2026-10-18 — Giới hạn SLM cục bộ (Qwen3-1.7B trên Jetson AGX Xavier)

> **Bối cảnh:** ViVi theo kiến trúc AI thích ứng (Cloud ↔ Local ↔ Rules). Đường chạy local có nhiệm vụ duy trì chức năng khi xe mất Internet. Model thử nghiệm: **Qwen3-1.7B Q4_K_M** chạy bằng `llama.cpp`, offload đầy đủ 29/29 lớp lên GPU. Cùng thiết bị còn có Whisper-small (STT) và ZeroTTS (TTS).

### Kết quả benchmark chính

| Hạng mục | Kết quả |
|----------|---------|
| SLM thô — TTFT p50 | **145 ms** |
| SLM thô — tổng cho ≤96 token | 455 ms |
| Tốc độ sinh | 46,7 token/giây |
| Pipeline schema **strict** | **0/10 lượt** do model phục vụ |
| Latency cả lượt (strict) | p50 35,8 s · p95 48,3 s |
| Pipeline schema **nới lỏng** | 7/10 lượt do model phục vụ |
| Accuracy khi model phục vụ | **2/7 lượt đúng route** |
| Latency model (nới lỏng) | p50 9,1 s |
| OpenRouter Qwen3.7 Flash | 77,2% route accuracy (57 câu dev) |
| OpenRouter latency | p50 6,0 s · p95 12,6 s |

### Phát hiện chính

**Vấn đề không phải "GPU chậm" hay "model quá nhỏ".** Qwen3-1.7B bắt đầu sinh token sau ~145 ms và hoàn thành câu ngắn trong <1 s. Điểm nghẽn xuất hiện khi model phải xử lý contract đầy đủ của pipeline.

#### 1. Contract đầu ra quá lớn so với tác vụ
Câu "Chỉnh điều hòa 25 độ" bị bọc trong:
- System prompt + persona ViVi
- Quy tắc hội thoại và an toàn
- Trạng thái tác vụ hiện tại + lịch sử hội thoại
- Catalog capability xe + schema `SemanticTurn` đầy đủ
- Quy định sử dụng tool và xử lý lỗi

→ Prompt semantic ~**8.000 token** cho một câu vài từ. Schema strict buộc model ghi ~25 trường/action (nhiều trường `null`), output ~**550 token**. Với 32 tok/s trong pipeline, riêng output đã ~17 s.

Kết quả: 10 lượt strict → **5 ReadTimeout + 5 ValidationError**, 0 lượt được model phục vụ.

#### 2. Pipeline giao quá nhiều trách nhiệm cho 1 model
SLM phải đồng thời: hiểu tiếng Việt → phân loại intent → chọn capability → trích tham số → hiểu state nhiều lượt → xử lý phủ định/sửa lệnh → sinh JSON đúng schema → tuân thủ safety rules.

Nới lỏng schema giảm completion từ 554 → **80 token** và model phục vụ được 7/10 lượt. Nhưng chỉ **2/7 đúng route** → bộc lộ hai giới hạn tách biệt:
- Schema strict ⇒ vấn đề latency + format
- Nới schema ⇒ lộ giới hạn **hiểu nghiệp vụ** của model 1.7B tổng quát

#### 3. Rút prompt cơ học không đủ
Bỏ persona + trường rỗng → prompt từ 8.000 xuống **6.900 token**. Kết quả **tệ hơn** (2/10 lượt). Nội dung dài trong prompt đang cung cấp ràng buộc cần thiết. Prompt cache của llama-server đã hoạt động (~8.000 token được cache) — bật thêm cache không phải giải pháp chính.

#### 4. Tranh chấp tài nguyên trên Jetson
Khi STT + SLM + TTS chạy song song:
| Metric | Riêng lẻ | Đồng thời |
|--------|---------:|----------:|
| LLM TTFT | 145 ms | 286 ms |
| Tốc độ sinh | 46,7 tok/s | 27,9 tok/s |
| TTS TTFA p95 | 125 ms | 757 ms |
| TTS RTF p95 | 1,3 | 2,6 |
| GPU peak | - | **99%** |

Scheduler ưu tiên STT → LLM và TTS gánh phần lớn độ trễ. Đây là giới hạn **kiến trúc dùng chung GPU**, không chỉ của model.

### Đánh giá lại giả định "cần model ≥20B"

Kết luận "dưới 20B không dùng được" **chưa được benchmark chứng minh**. Jetson có ~14,9 GB bộ nhớ dùng chung; tải đồng thời đã dùng tối đa ~9,27 GB. Một model 20B lượng tử hóa vẫn ngốn phần lớn ngân sách bộ nhớ trước KV cache, runtime, STT và TTS — có thể làm latency **tệ hơn**.

**Cách diễn đạt chính xác:** Model 1.7B tổng quát hiện chưa đủ accuracy cho **toàn bộ** pipeline nghiệp vụ ViVi. Model lớn + phần cứng mạnh là **một hướng**, không phải điều kiện duy nhất. Với contract nhỏ, dữ liệu đúng miền và lớp deterministic phù hợp, model nhỏ vẫn đảm nhận được phần đáng kể.

### Nguyên nhân câu "hãy subscribe"

**KHÔNG phải lỗi SLM.** Đây là hallucination của **Whisper STT** khi xử lý clip im lặng/nhiều nhiễu. Fix:
- Dùng VAD loại khoảng lặng
- Cắt audio trước khi gửi STT
- Điều chỉnh ngưỡng no-speech
- Chạy với `--no-context`
- Gán nhãn thủ công bộ audio thật trước khi tính WER/CER

### Giải pháp đề xuất

| # | Hành động | Tác động kỳ vọng |
|---|-----------|------------------|
| 1 | **Không gọi SLM cho mọi lệnh** — fast-path deterministic cho "đặt điều hòa 25", "mở cửa kính", "tắt nhạc"; SLM chỉ gọi khi mơ hồ | Latency đơn lệnh ~ms, tải SLM giảm mạnh |
| 2 | **Thu hẹp vai trò model** — SLM chỉ đề xuất `climate.set_temperature(value_celsius=25)`; backend giữ task state, validation, safety, execution | Output vài chục token thay vì 550 |
| 3 | **Dynamic tool selection** — câu có "điều hòa" chỉ gửi tool climate, không gửi full catalog | Prompt ngắn, nhiễu giảm, accuracy chọn tool tăng |
| 4 | **Thiết kế lại schema** — chỉ bắt buộc trường cần thiết, dùng native function calling, chia theo nhóm nghiệp vụ | Completion 80 token (đã kiểm chứng) |
| 5 | **Dữ liệu đúng miền** — câu lệnh cabin theo vùng miền, lỗi STT có chủ đích, hội thoại nhiều lượt, hard negative action/handbook/conversation | SFT/LoRA/distillation; model nhỏ chuyên miền > model lớn tổng quát |
| 6 | **Chỉ nâng model/HW sau khi tối ưu pipeline** — benchmark 3B, 7B, model chuyên tool-calling theo cùng golden dataset | Quyết định có bằng chứng, không theo cảm giác |

### Hạn chế của benchmark hiện tại

- Bộ OpenRouter: 57 câu dev, nhãn draft
- Bộ local: chỉ 10 câu
- Chưa có tập held-out độc lập
- Audio thật chưa gán nhãn thủ công → WER/CER chưa dùng chính thức
- Chưa benchmark model local đã fine-tune theo miền
- Chưa đo E2E hoàn chỉnh (end-of-speech → first TTS byte) trên pipeline local ổn định

Vì vậy kết quả hiện tại **đủ để xác định điểm nghẽn + định hướng kỹ thuật**, nhưng chưa đủ để tuyên bố một ngưỡng model tối thiểu.

### Bài học rút ra

Hiệu quả hệ thống AI **không chỉ phụ thuộc kích thước model** — mà toàn bộ thiết kế gồm phần cứng, runtime, prompt, schema, dữ liệu, routing và lớp deterministic.

Trong ô tô:
- **Rules** — lệnh đơn giản cần phản hồi tức thời
- **SLM** — hiểu ngôn ngữ phạm vi giới hạn
- **Cloud LLM** — hội thoại phức tạp khi có Internet
- **Backend** — luôn giữ quyền kiểm tra, xác nhận, thực thi
- **Mọi provider** — cần fallback và telemetry

Mục tiêu **không phải** buộc một model duy nhất giải quyết toàn bộ bài toán. Kiến trúc đúng là kết hợp AI (phần ngôn ngữ khó) + phần mềm deterministic (tốc độ, đúng đắn, an toàn).

### Những gì nhóm đã làm được (bằng chứng định lượng)

- Tích hợp Qwen3-1.7B Q4_K_M trên Jetson AGX Xavier, 29/29 lớp lên GPU
- Gateway riêng cho STT / SLM / TTS; chuyển đổi Cloud ↔ Local ↔ Rules
- Timeout, circuit breaker, fallback → model lỗi không mất khả năng điều khiển cơ bản
- Benchmark 57 câu OpenRouter + biến thể local trên Jetson
- Đo riêng TTFT, tok/s, latency, tỉ lệ model thực sự phục vụ
- Thử schema strict, nới lỏng, prompt rút gọn
- Xác nhận prompt cache hoạt động
- Đo resource contention khi STT+SLM+TTS chạy đồng thời
- Phát hiện hallucination Whisper trên im lặng/nhiễu
- Giữ validation, guardrail, HITL, quyền thực thi ở backend
- Dashboard quan sát từng stage, nhận biết chính xác khi model fallback

> Giá trị lớn nhất của quá trình này không chỉ là bản demo chạy được — mà là **bằng chứng định lượng về giới hạn thực tế của từng thành phần**, làm cơ sở quyết định kỹ thuật cho giai đoạn sau.

---

<!-- Thêm Week 6 sau Demo Day: retrospective + bài học rút ra -->
