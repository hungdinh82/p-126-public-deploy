# Worklog — The Odysseans (Nhóm P-126)

> Lịch sử phát triển **VIVI Cabin Copilot** (Mã đề DEV-01). Nguồn: ghi chú họp nhóm, mentor duty + `git log` trên nhánh `main` của [AI20K-Build-Phase-Cohort-4/P-126](https://github.com/AI20K-Build-Phase-Cohort-4/P-126).

## Thành viên

| Vai trò | Họ tên | MSSV | GitHub |
|---------|--------|------|--------|
| 🧭 Trưởng nhóm | Đinh Văn Hùng | 2A202602443 | [@hungdinh82](https://github.com/hungdinh82) |
| Thành viên | Nguyễn Thanh Phong | 2A202602843 | [@kaisye](https://github.com/kaisye) |
| Thành viên | Nguyễn Minh Dương | 2A202602920 | [@minhduong814](https://github.com/minhduong814) |
| Thành viên | Hoàng Trung Khải | 2A202602947 | [@khaihoang004](https://github.com/khaihoang004) |

## Tổng kết đóng góp (git shortlog toàn repo)

| Thành viên | Commit |
|------------|-------:|
| Đinh Văn Hùng (@hungdinh82) | 73 |
| Hoàng Trung Khải (@khaihoang004) | 72 |
| Nguyễn Minh Dương (@minhduong814) | 50 |
| Nguyễn Thanh Phong (@kaisye) | 48 |

> Cập nhật `git shortlog -sn --all` ngày 2026-10-18.

---

## 2026-09-20 — Gate 1 push

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Chốt scaffold repo, mở PR #1 từ nhánh `dev/nmduong`, push "gate 1" | ✅ | Repo + Codeowners khởi tạo |

---

## 2026-09-22 → 2026-09-23 — ViVi local voice assistant + MQTT simulator

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Phong | Upload UI đầu tiên; viết feature `vivi-local-assistant` (local voice pipeline) | ✅ | 2 commit nền tảng UI |
| Phong | Giảm latency pipeline STT-LLM-TTS bằng kỹ thuật streaming; thêm **LLM router dashboard**; buffer VIVI streaming audio chống dropout; fix voice Mai-Chi | ✅ | 6 commit, giảm latency 10s → ~2s |
| Khải | Thêm **orchestration contracts + safety flow**; traced turn orchestration; handle backend confirmations ở frontend; docs LangGraph & LangChain | ✅ | 4 commit đặt nền contract state + router |
| Hùng | Feat: **MQTT simulator integration** — nối ViVi với simulator qua MQTT | ✅ | Vehicle Simulator standalone + MQTT (Mosquitto) |

**📝 Note nhóm 23/09:**
- **Dương:** Tài liệu RAG đã full nhưng bằng tiếng Anh, đang chunking. Thử embedding đa ngôn ngữ. (Update: đã có default tài liệu tiếng Việt luôn.)
- **Khải:** Hoàn thiện contract state và router cho các luồng hội thoại, cẩm nang và điều khiển xe. Bổ sung **safety gateway R0–R3**, cơ chế xác nhận cửa/kính và trace theo từng lượt. Đã thêm test cho routing và confirmation.
- **Phong:** Fix lỗi giọng đọc (Mai-Chi). Chuyển sang streaming gen token → giọng nói → model → **giảm độ trễ từ 10s xuống 2s**. LLM thêm cấu hình chọn model.
- **Hùng:** Đã làm được Vehicle Simulator và MQTT, nối simulator với MQTT, nối ViVi với simulator qua MQTT. **Chưa làm:** bước xác nhận để cho phép điều khiển cửa kính/cửa xe qua hội thoại, intent hội thoại cho ghế/cửa → nên lệnh cửa kính trên UI bị chặn có chủ đích.

---

## 2026-09-25 — Vehicle confirm flow + RAG LangGraph

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Feat: **confirm cabin controls** — ViVi hỏi lại, chỉ gửi lệnh MQTT khi user trả lời "Xác nhận"; có "Hủy", timeout 30s, không reuse xác nhận cũ | ✅ | PR #5 merged |
| Hùng | Intent hội thoại cửa xe (mở/đóng, khóa/mở khóa); intent sưởi ghế (0-3, tắt) | ✅ | 34 test đạt |
| Dương | Feat: **build RAG system với LangGraph** | ✅ | Nhánh `integ/rag-and-vivi-voice` |
| Phong | Fix voice stream | ✅ | Pipeline voice ổn định |

**📝 Note nhóm 25/09:**
- **Hùng:** Đã bổ sung bước xác nhận an toàn cho cửa kính và cửa xe. Thêm intent cửa xe (mở/đóng, khóa/mở khóa) và sưởi ghế (0-3). Bổ sung kiểm tra an toàn: không mở cửa khi đang lái hoặc khóa, không khóa khi cửa mở. ViVi xác minh chặt hơn: sau ACK MQTT còn đọc lại state xe trước khi báo thành công. UI không còn tự giả lập khi backend/MQTT chưa sẵn sàng. **34 test đạt.**
- **Khải + Dương:** Đã tích hợp luồng LangGraph nối RAG system. Nối tiếp 2 model STT + TTS, **nhưng khá lag khi chạy local**.
- **Phong:** (1) Thiết kế UI/UX ViVi với trợ lý 3D chuyển động theo trạng thái nghe/suy nghĩ/thực hiện/phản hồi, hỗ trợ responsive và giảm chuyển động. (2) Backend FastAPI local: Microphone → PhoWhisper → LLM → Safety Gateway → Vehicle Simulator → ZeroTTS, lưu audio và transcript cục bộ. (3) Tích hợp nhiều LLM provider (OpenAI, Google, Local API, Rules fallback), chọn model trên UI, structured output. (4) **Tối ưu PhoWhisper Medium trên MacBook Air M1** bằng MPS, FP16, preload, warm-up, greedy decoding → latency **22,02s → ~1,42s**. (5) Streaming LLM → ZeroTTS theo mệnh đề, bắt đầu xử lý giọng ở ~2,2s; **23/23 test core/API đạt**.

**🎯 Note Week 2 (tổng kết):**
- Hùng: Hoàn thiện Vehicle Simulator chạy độc lập, state SQLite, MQTT qua Mosquitto. ACK + verify state trước khi báo thành công. Điều khiển cửa/kính/ghế với confirm "Xác nhận/Hủy".
- Khải + Dương: Tích hợp LangGraph + RAG + STT + TTS, lag khi chạy local.
- Phong: UI responsive + trợ lý 3D + pipeline FastAPI local + multi-provider + PhoWhisper 22,02s → 1,42s + streaming TTS 2,2s.

---

## 📍 2026-09-25 — Mentor duty Week 2 (anh Quốc)

**Feedback:** *"MVP tối ưu chất lượng trước, latency sau. 1 người tìm hiểu độ chính xác, build benchmark..."*

---

## 2026-09-26 → 2026-09-28 — Refactor cấu trúc + CUDA ZeroTTS

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Dương | Merge vehicle simulator vào main; add CUDA provider cho ZeroTTS; refactor v1 27-09 → v2 28-09 stable; move `server/` into `src/`; fix hook cho codex; clean duplicated files | ✅ | 6 commit refactor nền |

---

## 📍 2026-09-28 — Mentor duty (feedback gọi thêm tool)

- Đọc file structure trước
- Hỏi đáp RAG — đến mức như nào?
- Call action gọi tool (chỉnh điều hòa, nhạc, ghế, cửa sổ, mở cốp...)
- Hỏi metric tính khả thi (thời gian mắt rời khỏi kính lái, không phải xuống nhìn điều hòa)
- **Use case mới:** Call cứu hộ khi gặp sự cố, hỏi mọi người có ổn không; **chống buồn ngủ** (đọc số trên màn hình, tương tác, siết đai an toàn); hỏi trạm xăng gần nhất
- Vấn đề build benchmark: dataset chuẩn, review xong test, đánh giá lại

**Công việc tiếp theo (phân công sau mentor duty):**
- **Khải:** Tối ưu CUDA, version requirement, Jetson
- **Dương:** Benchmark RAG, system prompt, rail
- **Phong:** Làm UI, làm từ đầu local SLM, fix voice TTS
- **Hùng:** GitHub Action CI/CD, benchmark speech (tạo dataset voice ZeroTTS với nhiễu: trời mưa, tiếng ồn xe cộ...), nghĩ use case mới, mentor duty + Jira. UI tình trạng xe (4 cửa, cửa sổ, sưởi ghế, áp suất 4 lốp + alert engine), API reset trạng thái xe.

---

## 2026-09-29 → 2026-09-30 — CI/CD + voice path

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | CI: replace hosted workflows với local gates; add local quality gates và releases | ✅ | `.github/workflows/` với quality gates |
| Hùng | Change path voices ViVi | ✅ | Chuẩn hóa asset voice |

---

## 📍 2026-09-29 — Mentor duty

**Feedback:**
- Luồng LangGraph nối vào chạy local tối ưu
- Jetson Orin Nano hoặc Orin NX (tốt hơn Nano cũ)
- **3 use case ưu tiên:**
  1. Điều khiển điều hòa mở rộng bằng giọng nói
  2. Hỏi tình trạng xe bằng giọng nói (pin, áp suất lốp)
  3. **Nâng cao:** Cảnh báo chủ động về tình trạng bất thường của xe

**Chốt công việc:**
- **Khải:** Test deploy trên edge
- **Hùng:** Màn hình role Kỹ sư, deploy lên web gọi full API
- **Phong:** UI + call API (STT → LLM → TTS)
- **Dương:** Investigate lại LangGraph, tối ưu hạn chế multi-agent, thiết kế backend chuyển qua lại local ↔ API

---

## 2026-10-01 → 2026-10-03 — Proactive vehicle monitoring + mentor review fixes

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Merge PR #7 (integ/rag-and-vivi-voice) | ✅ | RAG tích hợp vào main |
| Hùng | Feat: **proactive vehicle monitoring**; CI local quality gates; fix mentor review findings (vehicle) | ✅ | PR #8, cảnh báo chủ động |

---

## 📍 2026-10-03 — Họp nhóm

**Feedback:** Chủ ý **demo ngắt mạng → luồng demo chuyển sang offline mượt mà** sẽ gây ấn tượng. Không trình bày "2 tính năng on/offline" — biến thành câu chuyện: *"đang dùng on ngon thì mất mạng → rút dây mạng giữa chừng trên sân khấu"*.

**Hướng:** Nếu phần cứng không được thì chạy local laptop. Chạy được offline, chưa cần trên phần cứng thật. **Chốt kịch bản demo và hạn chế tính năng mới.**

**Chốt công việc (phân công 03/10):**
- **Khải:** Test thử Whisper Small trên phần cứng, đo infer trung bình cho sample 10-15 từ, đảm bảo <5s
- **Phong:** Tối ưu Whisper CBB, xây dựng lớp API Gateway kết nối LangGraph; **chọn Whisper Small (0.5B) cho STT**
- **Dương:** Investigate LangGraph, tìm framework thay thế hoặc tối ưu, chuẩn hóa chuyển đổi API ↔ Local
- **Hùng:** Deploy lên web, hỗ trợ giao diện người dùng/kỹ sư

**Quyết định:** Sử dụng **Whisper Small (0.5B)** cho STT. Router ~1 triệu tham số cho gateway thay vì if-else.

---

## 2026-10-04 — TOML config + edge hardware monitoring + Soniox STT

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Khải | Feat: move runtime settings vào TOML config; enhance GPU metrics sampling; track per-turn pipeline timings; add edge hardware monitoring page | ✅ | PR #12, #13 — dashboard giám sát edge |
| Phong | Feat: add **Soniox live STT** + OpenRouter LLM provider; announce wait trước câu RAG chậm | ✅ | PR #9, #10 |

---

## 2026-10-06 → 2026-10-07 — Jetson GPU monitoring + Render deploy + RAG v2 + longterm memory

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Khải | Feat: edge hardware monitoring + command history; standalone ngrok launcher; **enhance Jetson GPU monitoring + telemetry** | ✅ | PR #14, #15, #16 |
| Hùng | CI: publish image và **deploy to Render**; cho phép Render image bootstrap; build public deployment image | ✅ | PR #17 — Live URL khả dụng |
| Dương | Feat: **re-build và benchmark RAG system**; add **longterm memory**; change system prompt cho ViVi thân thiện hơn; fix model classifier intent | ✅ | PR #18 — RAG v2 |

---

## 2026-10-08 — Mega-day: engineer dashboard + VietMap + VieNeu TTS

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Feat: **engineer local operations dashboard**; streamline safe vehicle confirmations; use hosted services trong cloud image; align provider catalogue checks | ✅ | PR #19, #20 |
| Phong | Feat: **VietMap navigation + 3D VF8 drive view**; **VieNeu TTS** với voice picker, cabin music, VinUni fallback, park-aware journey; Vietnamese-first Soniox STT với domain context + endpoint detection; live STT caption + body control panel; hood/trunk/open-close-all body controls; redesign Edge Monitor; restore role & engineer assets; speaker on by default | ✅ | PR #21, #22, #23, #24 — 9 commit |

---

## 2026-10-09 — Jetson Xavier CUDA bootstrap + benchmark v2

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Khải | Feat: **bootstrap Xavier CUDA models**; add authenticated STT/SLM/TTS inference service; feat jetson-ui với model status + interactive inference API tests; test(jetson) CLI smoke tests + deployment guide; feat(rag): fallback handbook FTS5 khi embeddings fail; feat(routing): select Jetson backend + enforce offline policy; docs: define laptop + Jetson AI migration; fix: add missing cusparse, pin Eigen checkout, change library version; feat(dev) launch 3 GPU models với API test UI; **feat(jetson): implement ARM CPU-ID bounds fix** | ✅ | 11 commit — Jetson chạy được stack |
| Dương | Test: unit test; **benchmark v2 cho new workflow**; fix new workflow LangGraph | ✅ | 3 commit refactor workflow |

---

## 2026-10-10 — Streaming SLM/STT/TTS + navigation + scene intersections

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Khải | Feat(ai): **optional streaming cho SLM STT và TTS** + streaming controls, live model output; docs streaming APIs + setup; feat normal PC + Jetson server lifecycle commands; feat allow private gateway operation không service token; test(api) isolate graph doubles từ AI gateway availability; feat(ai) configure private AI gateway với single server URL; docs(jetson) main integration + deployment checks; style(tests) sort imports; fix(jetson) integrate routing + fallback với unified workflow | ✅ | PR #25, #27 — 10 commit |
| Phong | Feat: **street network, turn signals và 4-window control** | ✅ | PR #26 |
| Dương | Feat: **navigation and charge planning** | ✅ | PR #28 |

---

## 2026-10-11 — Wake-word + navigation step holds

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Phong (@kaisye) | Feat: **wake-word dataset, training và voice recorder**; wake-word, follow-up listening + spoken confirmations; fix nearest/trip suffix parsing, compound search guard; fix(navigation): hold chained steps cho route consent | ✅ | PR #29, #30, #31 |
| Dương | Feat: clear conversation history; benchmark + clean temp files | ✅ | Housekeeping |

---

## 2026-10-12 → 2026-10-13 — Internet search Tavily + Scenario Lab fix

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Dương | Feat: **internet search via Tavily API**; extend search limit long trip; parse rule destination name | ✅ | 3 commit |
| Hùng | Fix: Scenario Lab cho memory runtime; fix .dockerignore; nhiều merge main → public-build | ✅ | PR #33 |
| Khải | Feat(tts): add experimental **ZeroTTS INT8 setup profile** | ✅ | Baseline voice latency |

---

## 2026-10-14 — Adaptive online/offline gateway + guardrails internet search

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Fix: ps lấy RSS từ `/proc` cho macOS; merge PR #34, #35 | ✅ | Monitor cross-platform |
| Hùng | Feat: **add adaptive online-offline AI gateway** | ✅ | Tầng Google → local |
| Dương | Feat: **guardrails cho internet search**; baseline deploy; giữ context conversation | ✅ | 3 commit |
| Khải | Feat(tts): TensorRT INT8 cho AGX Xavier; fix guard TensorRT 8.6 APIs trên Xavier 8.5; **connect laptop STT và warm up remote SLM prompt**; finalize INT8 TTS runtime + expose model RAM trong test UI | ✅ | PR #34 |

---

## 2026-10-15 → 2026-10-16 — Jetson realtime voice + form-owned model

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Mô tả repo + hiển thị flow call ở dashboard kỹ sư | ✅ | `docs/REPOSITORY_STRUCTURE.md` |
| Dương | Refactor: **unified LLM gateway**; cancel navigation plan; keep navigation guide data | ✅ | Core v2 contract |
| Khải | Model-owned request forms + validated submission; **benchmark Xavier TTS profiles** + optional mixed FP16 CUDA; preserve grounded continuations + journey consent; ground voice revisions + cancel stale Jetson inference; guard voice commands từ generic model replies; recognize named AC temperature changes; remove added offline grammar vetoes; restore main UI + semantic contract với isolated local runtime budgets; use documented local tools với bounded discovery + revision grounding; fix local pending form lifecycle + document public tools; merge adaptive gateway while preserving Jetson voice pipeline; implement model-owned request forms với validated submission; separate open/close form intents + ground remaining doors; fix model-owned music + window form interpretation | ✅ | PR #37 — 15 commit |

---

## 📍 2026-10-07 → 2026-10-09 — Note họp nhóm (chi tiết)

**07/10:**
- Sửa chunking, thêm metadata cho các chunk (RAG)
- Phương pháp retrieval + embedding
- Source crawl data đang thiếu nhiều thông tin
- Test thật nhiều, cần thêm kịch bản test
- Xử lý input âm thanh (giảm nhiễu, lọc dưới ngưỡng)
- Ngắt lời khi đang nói dở
- Conversation tool search internet
- Tính năng cảnh báo chủ động thành giọng nói
- TTS API
- Xem lại đo phần cứng (32GB RAM?)

**Expect anh em làm:**
- Pipeline nhỏ preprocessing audio input (lọc ồn)
- Cấu hình ViVi: chọn input mic + output audio
- Feat: chỉ đường — nối con VietMap

**Big update cho Hùng:** Toàn bộ thông số của xe có thể điều chỉnh manual trên giao diện → để ViVi **ping chủ động đến tài xế** khi gặp vấn đề (áp suất lốp, pin, lịch bảo dưỡng/đăng kiểm). Thêm trạng thái cốp đóng/mở.

**📝 Tích hợp VieNeu TTS (ghi chú kỹ thuật):**
- Giọng đọc chạy trên cloud, truyền trực tiếp về trình duyệt, bắt đầu phát sau ~1s
- Giọng mặc định: Mai Bé Phương; đổi sang **Tường Vy** trong `config.toml`
- Key `VietNeuTTS_API_KEY` trong `.env` vẫn được đọc
- Hai nút chọn giọng: **ViVi** (ZeroTTS trên máy) và **VieNeu** (cloud)
- Vị trí mặc định: **VinUni** (trước là Hồ Gươm) — chỉnh được trong `config.toml`
- Khi định vị độ chính xác cao bị lỗi, ViVi tự thử lại bằng định vị Wi-Fi
- Chuyển về P → bản đồ dừng (trước đây mô phỏng vẫn chạy)
- Nhạc trong xe: đoạn nhẹ nhàng tạo bằng Web Audio (không vướng bản quyền); tắt khi ViVi đang nghe, nhỏ lại khi ViVi nói
- Xe giữ khoảng cách xa hơn: tối thiểu 9m → **15m**; chuyển làn khi cách xe chậm 42m → **50m**

**📍 Họp 09/10:**
- **Phong:** Tính năng gọi wakeup (Mai)
- **Dương:** Test tính năng chỉ đường, multi-task (ví dụ: tìm quán cà phê gần đây + bật điều hòa cùng lúc)
- **Khải:** Test/benchmark các model local (SLM Qwen1.7B), test chạy 2 service đồng thời
- **Hùng:** Test full luồng "willing user khó tính"

**Thêm:**
- Cho phép truy cập internet search
- Render deploy đang: 0.1 CPU, 512 MB RAM
- Thêm mã thiết bị khi vào sạc
- Thêm giới thiệu sản phẩm vào trang
- Benchmark xong gửi anh xem (3 chế độ: online, offline, degraded)
- Diễn tập kịch bản demo
- Quay video demo ở phần cứng
- Tối ưu SLM cho thông minh hơn
- Gateway offline/online
- Gửi app chạy demo lên Discord

**🔥 Phát hiện quan trọng:** SLM timeout ở model local vì **model 1.7B tham số quá yếu**. Prompt "Chỉnh điều hòa 25 độ" + system prompt "Bạn là trợ lý ViVi..." rất dài → timeout. Không fix được với model <20B tham số (xem [benchmark deep-dive trong JOURNAL](JOURNAL.md#deep-dive-2026-10-18--giới-hạn-slm-cục-bộ-qwen3-17b-trên-jetson-agx-xavier)).

**Chốt phân công cuối tuần:**
- **Dương:** SLM, LLM, LangGraph
- **Phong:** Benchmark cả bản small lẫn bản tối ưu; viết kịch bản quay video demo; làm slide (phải có kiến trúc)
- **Khải:** Chỉnh lại STT, TTS default INT8 CPU đủ trống RAM để cache prompt; hard fix output, define tool, well-define schema
- **Hùng:** Login theo account, tách session; thêm mô tả visualize các button; thêm phần "mớm cho tôi các câu khi xác nhận" (VD "Mở cửa nào? Ghế tài, phụ, sau trái, sau phải?")

---

## 2026-10-17 — Unified gateway + schema cứng cho local SLM

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Fix session phone & pin, fix voice Tường Vy, flow dashboard kỹ sư | ✅ | Session state ổn định |
| Dương | Refactor: **unified LLM gateway**; dùng JSON grammar đơn giản context; fix local schema; re-define tools thành đơn vị nhỏ | ✅ | Schema-lock cho SLM, PR #39 |
| Khải | Tiếp tục iterate local-agent form grounding | ✅ | 7 commit trong ngày |

---

## 2026-10-18 — Hợp nhất navigation + so sánh model local/cloud + Demo Day prep

| Member | Task | Status | Output |
|--------|------|--------|--------|
| Hùng | Merge PR #40 (fix/local-slm) và PR #41 (feat/navigation); hiển thị button hướng dẫn + fix cảnh báo pin, hiển thị tên trường kỹ thuật; thêm mô tả button và hướng dẫn sử dụng | ✅ | Hai nhánh chính hợp nhất, build xanh |
| Dương | So sánh model local vs Google; fix: ask new user for location; cập nhật navigation logic (open drafts, improve journey management); fix handling search và cancel flow | ✅ | Benchmark compare + UX fix |
| Khải | Tách tool local thành đơn vị nhỏ (feat/local-model: reversible Vietnamese labels, feat/vehicle: expose separate measured-status tools, feat/local-agent: ground form slots + validate separate tool proposals, test fixtures: configure semantic resolver budget, test: feature-based agent benchmark + failure reports, fix dialogue: accept bound whole-answer confirmation variants, docs: describe separate local tools + grounded form contracts) | ✅ | 7 commit — `docs/local_tool_pipeline.md` |

---

## Cách cập nhật

```bash
# Thêm block ngày mới theo format trên
# Hoặc export commit mới:
git log --since="YYYY-MM-DD" --pretty=format:"%ad | %an | %s" --date=short >> WORKLOG.md

# Shortlog toàn repo:
git shortlog -sn --all
```
