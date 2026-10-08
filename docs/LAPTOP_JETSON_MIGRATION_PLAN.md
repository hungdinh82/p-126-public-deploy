# Kế hoạch chuyển ViVi sang Laptop Orchestrator + Jetson AI server

> Trạng thái: kế hoạch triển khai, **chưa phải chức năng đã có**. Kiến trúc và chuẩn giao tiếp đích nằm trong [SYSTEM_WORKFLOW.md](../SYSTEM_WORKFLOW.md), đặc biệt mục 43. File này là checklist để giao cho model/nhóm triển khai theo từng PR nhỏ.

**Checkpoint code (nhánh `feat/jetson-ai-pipeline`, chưa triển khai lên AGX):** Có `jetson_ai/` với auth, liveness, capability từng model, canonical LLM và OpenAI-compatible chat; STT upload Whisper-base và TTS ZeroTTS/VIVI đã có engine thật. `scripts/jetson_ai.sh` setup/run ba model bằng môi trường riêng, pin GGUF/revision speech, build CUDA Xavier và chỉ hoàn tất khi TTS CUDA smoke đạt. Laptop dùng lại provider `local` qua env; router opt-in chọn backend trước graph; `sqlite_local` fallback FTS5 khi embedding thiếu/lỗi runtime. Test gateway, policy router và RAG đã có. Remote STT/TTS adapter vào Laptop, fallback giữa lượt, canonical speech envelope và trace chi tiết vẫn chưa hoàn thành. Profile ORT 1.16.3 cho CUDA 11.4 cần xác minh trên AGX; smoke GPU x86 không thay cho smoke Xavier. Trang `/test` theo dõi trạng thái và kiểm thử inference; CLI pipeline dùng cho các nhánh RAG/tool trên Laptop. Hướng dẫn: [jetson_ai.md](jetson_ai.md). Giữ checklist chưa đánh dấu cho đến khi đủ acceptance của cả mục.

**Checkpoint test local:** `bash scripts/test_local_ai.sh` tự khởi động llama.cpp + gateway `jetson_ai` với model Qwen3-1.7B Q4 thật trên x86 CPU, mở prompt nhập text và dừng process khi thoát. `--target pipeline` đi qua API `/api/v1`, router, LangGraph và memory simulator; model trực tiếp trả lời được, climate/confirmation chạy được. Smoke đã phát hiện model chọn sai zone cửa sổ và phân loại handbook thành unsupported; chưa đạt accuracy nghiệp vụ. Script và kết quả chi tiết nằm trong hướng dẫn trên. Đây chưa phải smoke trên Xavier.

## 1. Kết quả cần đạt

Browser chỉ gọi API ViVi trên Laptop. Laptop giữ LangGraph, session/history/memory, handbook RAG, kiểm tra bằng chứng, policy/xác nhận, vehicle gateway và MQTT. Jetson chỉ chạy server suy luận STT/LLM/TTS, không giữ session, không nhận lệnh xe, không có handbook hay MQTT credentials. Cloud AI là backend tùy chọn theo policy.

```text
Browser → Laptop API → LangGraph hiện có
                      ├─ RAG SQLite + E5/FTS5 trên Laptop
                      ├─ Safety/confirmation → vehicle gateway → MQTT
                      └─ AI adapters/router → Jetson STT/LLM/TTS hoặc cloud
```

Khi Jetson mất nhưng Internet còn: **Laptop RAG → cloud LLM**. Khi cả hai mất: Laptop vẫn nhận text, truy xuất handbook bằng E5/FTS5 (FTS5 khi E5 lỗi), dùng rules/extractive để trả lời có nguồn hoặc abstain. Điều này đòi handbook và model embedding nằm trên Laptop.

### Môi trường AGX Xavier đã kiểm tra qua SSH (09/10/2026)

Kiểm tra chỉ đọc trên alias `jetson`, đúng hardware `nvidia,jetson-xavier` (`aarch64`):

| Hạng mục | Giá trị quan sát | Hệ quả cho migration |
| --- | --- | --- |
| Hệ điều hành/kernel | Ubuntu 20.04.6 LTS; Linux `5.10.216-tegra`; L4T `R35.6.5` | Pin binary/container theo **L4T R35.6.5**, không lấy hướng dẫn Nano JetPack 4 hay binary x86/CUDA 12 của PC. Gói `nvidia-jetpack` meta không có trong kết quả kiểm tra nên không gán nhãn JetPack cụ thể khi chưa xác minh. |
| CUDA | Toolkit `11.4.315` ở `/usr/local/cuda-11.4`; cuDNN `8.6.0.166`, TensorRT `8.5.2` | `nvcc` có ở `/usr/local/cuda/bin/nvcc` nhưng chưa nằm trong `PATH`; script dùng đường dẫn đã kiểm tra và build/test binary tương thích CUDA 11.4. |
| Bộ nhớ/chế độ điện | 14 GiB RAM, khoảng 12 GiB available lúc kiểm tra; 7.3 GiB swap; `MODE_15W_DESKTOP` | Đo lại khi chạy STT+LLM+TTS; không tính swap là RAM model. Benchmark phải ghi power mode. |
| Lưu trữ | Root 28 GiB, còn 3.5 GiB; `/mnt/jetson-data` 59 GiB, còn 52 GiB và user `p126` có quyền ghi | Mọi model, cache, source/build và venv **mới** đi vào `/mnt/jetson-data/vivi-ai`; `setup` dừng trước khi tải/build nếu mount mất hoặc không đủ chỗ. |
| Python | System Python 3.8/3.9; Python 3.12.15 của `uv` đã được dùng bởi `.venv` repo | Tạo venv **riêng** cho Jetson AI service, không cài thêm vào `.venv` đang dùng bởi runtime hiện tại. |
| Docker | Docker 26.1.3, NVIDIA container runtime có cấu hình, nhưng `p126` bị từ chối truy cập `/var/run/docker.sock` | Không lấy `docker run` làm đường `run` mặc định. Có thể dùng Docker sau khi quyền được cấu hình riêng; hai lệnh bắt buộc phải có đường native/user-space chạy được. |
| Artifact hiện thấy | Repo trên Jetson có E5 INT8 khoảng 130 MiB; chưa thấy `llama-server`/`whisper-cli` trên `PATH` | Không coi STT/LLM/TTS đã sẵn sàng. `setup` phải cài/kiểm tra từng capability và ghi rõ capability nào còn thiếu. |

Đây là snapshot, không phải benchmark tải hay xác nhận cả ba model cùng chạy. Recheck dung lượng, model, CUDA và Docker trước mỗi lần `setup`; không sửa driver/JetPack hệ thống trong migration này.

## 2. Phạm vi phải giữ nguyên

- Giữ public runtime `src.vivi.api.app:app`, các endpoint `/api/v1`, schema `TurnRequest`/`TurnResponse`, NDJSON `final`, `session_id`/`turn_id` và hành vi UI hiện tại. Nếu thật sự cần breaking change, làm `/api/v2` riêng; không đổi ngầm nghĩa field.
- Giữ các node/edge và nghiệp vụ của [LangGraph](../src/vivi/agents/graph.py): phân loại, hỏi rõ, task nhiều lượt, RAG evidence gate, citation validation, action validation, safety, confirmation, execute, verify và persist. Không viết graph thứ hai trên Jetson.
- Giữ các classifier/generator, rules, prompt, tool catalog và [RAG runtime](../src/vivi/rag/runtime.py) hiện có. Thay cách cấp dịch vụ bằng adapter; không sửa câu trả lời nghiệp vụ chỉ để phục vụ topology mới.
- Giữ [VehicleActionGateway](../src/vivi/vehicle/gateway.py), `domain/safety.py`, confirmation, MQTT simulator và contract trong [docs/contracts.md](contracts.md). LLM chỉ đề xuất; Laptop là nơi duy nhất thực thi và xác minh action.
- Giữ `scripts/setup.sh`, `scripts/run_edge.sh`, Dockerfile Nano và hướng dẫn Nano cũ dùng được cho profile hiện tại. Profile Jetson AI server mới có tên/script riêng; không biến profile cũ thành service mới một cách âm thầm.
- Không chỉnh `config.toml`/`.env` sẵn có của developer trong migration. Thêm cấu hình mẫu/profile mới và chọn bật bằng flag; `rules` + memory simulator vẫn chạy được không cần Jetson/cloud.

## 3. Ranh giới thay đổi nhỏ nhất

| Nơi | Cần thêm/đổi | Tránh làm |
| --- | --- | --- |
| `src/vivi/api/runtime.py` | Compose AI adapters, health/router ở Laptop | Khởi tạo model Jetson hoặc sửa nghiệp vụ graph tại đây |
| `src/vivi/providers.py`, `src/vivi/orchestration.py` | Chọn provider khả dụng theo policy; giữ `TurnResponse` | Cho Jetson quyết định workflow hoặc gọi tool |
| `src/vivi/agents/classifier.py`, `src/vivi/rag/generator.py`, `src/vivi/structured_llm.py` | Tái dùng `local` OpenAI-compatible LLM adapter; chỉ thêm wrapper nếu cần contract/timeout/trace | Viết lại prompt, planner, classifier logic |
| `src/vivi/speech/stt.py`, `src/vivi/speech/tts.py` | Thêm `RemoteJetsonSTT`/`RemoteJetsonTTS` cùng interface hiện có; giữ các adapter cũ | Đổi public STT/TTS API hoặc buộc mọi profile dùng Jetson |
| `src/vivi/rag/runtime.py` | Giữ retriever trên Laptop; thêm FTS5 fallback khi E5 không sẵn sàng | Chuyển handbook/index sang Jetson; tạo corpus thứ hai |
| `src/vivi/api/routes/health.py`, monitor | Báo riêng configured/reachable/model ready/degraded và route thực | Coi API Jetson online là mọi model đều sẵn sàng |
| `jetson_ai/` (package mới) | Private inference service, model adapters, health/capabilities | Thêm một ViVi public API/Orchestrator hoặc truy cập MQTT |
| `scripts/jetson_ai.sh` (mới) | Một entrypoint với subcommand `setup` và `run` | Gọi `scripts/setup.sh` PC hoặc yêu cầu setup handbook trên Jetson |

**Cách giảm tác động:** giai đoạn đầu cho Jetson gateway cung cấp thêm `/v1/chat/completions` tương thích OpenAI và kết nối qua `local_llm_base_url`/client hiện có; endpoint `/v1/llm/generate` giữ contract chuẩn của service. Sau đó đưa health/routing vào quanh provider hiện tại. STT/TTS dùng adapter mới đúng interface đang dùng. Chỉ tách port chung sâu hơn nếu những adapter đó chưa đủ; không refactor đồng loạt các node LangGraph.

## 4. Checklist triển khai theo thứ tự

### Giai đoạn 0 — Khóa baseline và phạm vi

- [ ] Chạy/ghi lại test, smoke và eval trên profile `rules`, cloud đang dùng, local LLM (nếu có), STT/TTS, RAG, xác nhận R2 và MQTT. Lưu commit/config không chứa secret cùng kết quả để so sánh.
- [ ] Recheck môi trường AGX Xavier theo bảng SSH ở trên: L4T, CUDA toolkit, power mode, mount `/mnt/jetson-data`, dung lượng trống và quyền ghi. Nếu khác snapshot, chọn lại manifest/runtime trước khi cài.
- [ ] Chốt một deployment đích: Laptop chạy API + handbook; Jetson AGX Xavier chạy private AI service. Ghi rõ profile Nano chạy toàn API là luồng độc lập.
- [ ] Chốt quyền dùng cloud theo môi trường (`offline_only`, `prefer_local`, `allow_cloud`) và quy tắc dữ liệu được gửi ra cloud. Giá trị mặc định phải giữ hành vi profile hiện tại khi chưa bật router mới.
- [ ] Viết contract tests cho public `/api/v1` và các kết quả `verified`, `confirmation_required`, `unverified`, `unsupported`, `error` trước khi đổi wiring.

### Giai đoạn 1 — Jetson AI service có thể chạy độc lập

- [ ] Tạo `jetson_ai/` với `GET /health/live`, `GET /v1/capabilities`, `POST /v1/stt/transcriptions`, `POST /v1/llm/generate`, `POST /v1/tts/synthesize` theo mục 43 của `SYSTEM_WORKFLOW.md`. Trong bước migration đầu, expose thêm `/v1/chat/completions` tương thích client local hiện có; gateway mỏng có thể gọi llama.cpp phía sau. Hai LLM endpoint phải cho kết quả tương đương qua contract tests.
- [ ] Capability trả `ready/loading/busy/unavailable` theo **từng model**, kèm `model_id`, version, queue depth, hỗ trợ stream và giới hạn đầu vào. Health process không thay cho readiness.
- [ ] Jetson kiểm tra input, timeout, số request đồng thời, audio length, token/output limit; phản hồi lỗi có mã máy đọc được. Hỗ trợ hủy request/stream và đóng process con khi nhận SIGTERM/Ctrl+C.
- [ ] Dùng build context/dependency riêng cho `jetson_ai/`; không copy handbook, SQLite history, API cloud key, MQTT credential hoặc vehicle code vào image/service Jetson AI. Không expose service ra Internet; xác thực Laptop ở ranh giới LAN.
- [ ] Chạy contract tests bằng fake model trước, rồi smoke trên model thật. Khi một model không cài/không nạp được, các capability khác vẫn có thể `ready`.

### Giai đoạn 2 — Hai lệnh vận hành trên Jetson

Giao diện CLI bắt buộc sau khi triển khai:

```bash
bash scripts/jetson_ai.sh setup --profile agx
bash scripts/jetson_ai.sh run
```

`setup` phải **idempotent**: phát hiện `aarch64`, L4T/CUDA/power mode và profile; kiểm tra mount `/mnt/jetson-data` cùng dung lượng cần theo manifest trước khi tải. Tạo `/mnt/jetson-data/vivi-ai/{venv,models,build,cache,logs}` thuộc user chạy service, dùng Python 3.12 user-space sẵn có hoặc bootstrap interpreter tương thích vào ổ dữ liệu; không đụng `.venv` repo. Chuẩn bị binary/model/voice theo manifest pin version + checksum, dùng CUDA 11.4 toolchain đã kiểm tra (hoặc CPU fallback được ghi rõ), tạo config mẫu và token xác thực nếu chưa có, chạy self-test từng capability, in URL LAN và vị trí file kết nối cho Laptop. Lần chạy lại không tải/build lại thứ đã hợp lệ; có thể dùng bundle model chuẩn bị trước khi Jetson không có Internet. Lỗi phải nói rõ bước nào hỏng và lệnh chạy lại. Không yêu cầu người dùng sửa `config.toml` hay chạy riêng từng model bằng tay trên Jetson.

`run` phải kiểm tra artifact/config/token và mount dữ liệu, khởi động private gateway và model process cần thiết theo profile **không cần Docker/sudo**, chờ readiness, in URL + trạng thái từng capability và power mode, chạy foreground và dừng sạch bằng Ctrl+C. Nếu STT/TTS không khả dụng, server vẫn chạy LLM với health phản ánh đúng; không báo toàn bộ hệ thống `ready` giả. Có thể bổ sung `status`/systemd về sau, nhưng hai lệnh trên phải đủ cho setup và chạy thử từ checkout sạch sau khi L4T/CUDA, repo và mạng đã sẵn sàng.

Chỉ dùng profile Nano 4 GB nếu đo được model tổ hợp phù hợp. Không hứa cả STT+LLM+TTS cùng resident trên Nano; profile AGX là mục tiêu chính. `scripts/jetson_ai.sh` mới không được chạy API ViVi, simulator hay broker.

### Giai đoạn 3 — Adapter và routing trên Laptop

- [ ] Thêm `jetson_ai_url`, auth secret, timeout và policy vào `Settings` qua env/profile riêng; secret ở `.env` và không commit. Không sửa giá trị cục bộ hiện có trong `config.toml`.
- [ ] Tạo remote STT/TTS adapter theo interface hiện tại; tái dùng local LLM client hoặc thêm remote LLM adapter mỏng trả đúng `IntentDecision`/`HandbookGenerator` contract. Mọi model output vẫn qua validate hiện có.
- [ ] Thêm health probe có TTL ngắn; phân biệt `configured`, `reachable`, `model_ready`, `busy`. Chỉ chọn capability `ready`, không chọn Jetson chỉ vì host ping được.
- [ ] Router nằm trên Laptop; đầu tiên chọn backend cho cả turn/AI stage trước khi chạy graph. Nếu cần fallback giữa các stage, làm qua adapter/provider interface mà không đổi thứ tự node. Không replay graph từ đầu sau khi có tool side effect.
- [ ] Giữ RAG trên Laptop: `sqlite_local` dùng E5+FTS5; khi E5 lỗi/missing, fallback `sqlite` FTS5 với cùng source IDs và evidence gate. Nếu handbook artifact thiếu/hỏng, trả trạng thái RAG unavailable rõ ràng.
- [ ] STT stream không tự tuyên bố có fallback streaming nếu remote adapter chỉ hỗ trợ upload. Khi stream lỗi, Browser/Laptop chuyển sang upload đã có và giữ `session_id`/`turn_id`; TTS lỗi vẫn giữ text.

### Giai đoạn 4 — Fallback, trace và UI

- [ ] Giữ `session_id`/`turn_id` công khai; Laptop tạo/reuse `request_id` cho cặp đó, `call_id` cho STT/LLM/TTS và tăng `attempt` khi thử backend khác. Ghi backend, model, lý do chọn, latency/lỗi/fallback vào trace/monitor; không log secret hoặc audio/raw history mặc định.
- [ ] Đặt deadline toàn lượt và timeout từng stage; retry có giới hạn cho AI inference/read. `offline_only` tuyệt đối không gọi cloud, kể cả khi Jetson chết.
- [ ] Chỉ fallback trước khi công bố kết quả/âm thanh hoặc phát lệnh xe. Sau khi MQTT command được gửi, chỉ đợi ACK/read-back theo gateway/idempotency hiện có; không gửi lại vì LLM/TTS lỗi.
- [ ] UI hiển thị text/rules mode, cloud mode, Jetson mode và capability suy giảm đúng health. Không thay layout/luồng người dùng nếu chưa cần.

### Giai đoạn 5 — Nghiệm thu và chuyển mặc định

- [ ] Contract tests: fake Jetson trả valid/invalid schema, `loading`, `busy`/429, timeout, 503, stream cắt giữa chừng; xác nhận adapter map lỗi và router chọn đúng.
- [ ] Chạy bốn tình huống Jetson × Internet. Riêng **Jetson DOWN + Internet UP** phải trả handbook answer từ Laptop RAG + cloud LLM; **cả hai DOWN** phải trả rules/extractive có citation hoặc abstain bằng text.
- [ ] Test hành động R0/R1/R2 khi đổi backend, confirm single-use/expiry/replay, MQTT ACK mất hoặc state lệch. Không được có hai lệnh xe cho một action; không báo “đã” trước verify.
- [ ] So sánh các test/eval ở giai đoạn 0; đo p50/p95 toàn lượt, STT/LLM/TTS, time-to-first-audio, RAM/CPU/GPU và thời gian khôi phục Jetson. Đặt timeout/concurrency thực từ số đo AGX, không đoán trước.
- [ ] Chỉ bật router mới làm mặc định sau khi profile cũ và fault tests đạt. Giữ flag/profile cũ để rollback bằng cấu hình mà không rollback dữ liệu hoặc đổi public API.

## 5. Definition of done cho model thực hiện migration

Một PR/giai đoạn chỉ được xem là xong khi có: thay đổi code giới hạn đúng ranh giới; contract/unit tests tương ứng; hướng dẫn chạy Laptop/Jetson; kết quả smoke trên profile cũ; kết quả fault case liên quan; ghi rõ tính năng Jetson nào thực sự chạy trên AGX và tính năng nào còn chưa có. Không ghi “đã hỗ trợ” trong README/health khi chỉ có cấu hình hoặc mock.

**Yêu cầu cho model nhận kế hoạch này:** đọc `SYSTEM_WORKFLOW.md` và `docs/contracts.md` trước khi code; triển khai từng giai đoạn theo thứ tự; bảo toàn core graph/RAG/safety/vehicle và public API; không xóa setup Nano/PC cũ; không chuyển session/RAG/tool sang Jetson; ghi checkpoint sau mỗi giai đoạn gồm file đã đổi, test đã chạy, rủi ro còn lại và bước tiếp theo, rồi tiếp tục công việc đã được giao nếu không có blocker thực sự. Không dừng để xin phép lại cho các thay đổi đã được giao.
