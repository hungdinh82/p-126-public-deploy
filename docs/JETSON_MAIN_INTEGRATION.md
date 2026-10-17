# Jetson với lõi LangGraph v2 — 16/10/2026

Kiến trúc runtime chuẩn nằm ở [langgraph_core_v2.md](langgraph_core_v2.md).
Bản ghép giữ một graph hội thoại, một `ModelGateway`, checkpoint `conversation-v4`
và API turn/navigation v2. Jetson chỉ phục vụ inference; task, câu hỏi, consent,
navigation và effect ledger vẫn do backend chính sở hữu.

```text
Browser → /api/v2/turn hoặc /api/v2/turn/stream
        → SemanticResolver → ModelGateway → cloud được chọn → local
        → reducer/scheduler → capability → effect ledger

STT/TTS → /api/v1/stt và /api/v1/tts[/stream] → cloud hoặc Jetson
```

## Điểm tích hợp từ main

- Giữ scheduler Jetson với hàng đợi riêng theo model, STT/TTS chia sẻ tài nguyên
  speech; LLM chậm không chiếm hàng đợi speech. Hủy HTTP phải hủy request inference
  đang chờ và giải phóng reservation; native decoder đang chạy được chờ kết thúc.
- Whisper nhận mỗi bản ghi với `no_context=true`, tránh lẫn câu của request trước.
  Device STT được cấu hình độc lập. TTS có kiểm tra RAM và tùy chọn FP16 rõ ràng;
  không tự chuyển sang FP16 hoặc thay artifact đang dùng khi conversion lỗi.
- STT fallback dùng lại audio gốc, báo provider thực sự phục vụ. TTS thử chunk đầu
  trước HTTP 200 và chỉ fallback trước khi phát PCM. Không chạy lại turn/effect
  khi TTS lỗi. UI giữ mute và cơ chế loại audio/event của lượt cũ.
- Connectivity Supervisor probe nền, có hysteresis, audit metadata và profile/
  scenario trong Engineer. Supervisor chỉ báo eligibility; `ModelGateway` sở hữu
  thứ tự gọi, breaker, deadline và trace cho semantic, địa điểm và sinh câu trả lời.
  Chưa có kết quả probe thì không chặn lời gọi. Một provider lỗi không đồng nghĩa
  toàn bộ Internet đã mất. Local 429/503 dùng cooldown tối đa một giây.
- Profile `auto` giữ cloud đã chọn → local; `cloud_only` lọc local;
  `jetson_only` lọc cloud; `fallback_only` không gọi LLM. Các profile không tạo
  provider giả. Chọn local trực tiếp chỉ gọi local; offline không gọi cloud.
- Khi offline, map/search mới bị chặn ở biên adapter; journey đã lưu và P/D vẫn
  đi qua navigation domain. Snapshot/consent/ledger không thay chủ sở hữu.

Không nhập pipeline local forms/tool discovery, parser sửa ý định, provider
`rules`/`auto`, public turn v1 hoặc voice inbox tự gộp transcript thành lượt mới.
Những đường này dùng contract cũ và chưa chứng minh được binding/replay theo v2.
Giới hạn semantic 20 giây, mỗi provider tối đa 10 giây, place decision 3 giây và
turn 120 giây vẫn áp dụng. `ai_server_request_timeout_seconds` chỉ dành cho
transport speech, không mở rộng budget model/turn.

## Cấu hình và chạy

Trong `config.toml`, dùng địa chỉ gateway thật của máy; ví dụ kết nối USB Jetson:

```toml
llm_provider = "google"
local_llm_model = "vivi-qwen3-1.7b"
ai_server_url = "http://192.168.55.1:8789"
ai_server_auth_required = true
jetson_ai_policy = "allow_cloud"
```

Đặt `AI_SERVER_TOKEN` trong `.env` theo token trên gateway; không ghi token vào Git.
Backend dùng cùng URL/token cho probe và inference. Muốn offline, đặt
`jetson_ai_policy = "offline_only"`. Với LM Studio/llama.cpp độc lập, dùng
`jetson_ai_policy = "disabled"`, `local_llm_base_url` và `local_llm_model`.
`prefer_local` là tên config cũ; muốn ưu tiên local hãy chọn provider `local`.

PC:

```bash
bash scripts/ai_server.sh setup
bash scripts/ai_server.sh run
# Terminal khác:
.venv/bin/python run.py
```

Jetson native:

```bash
bash scripts/jetson_ai.sh setup --profile agx
bash scripts/jetson_ai.sh run
```

Nếu backend ở Docker và gateway ở host, dùng `http://host.docker.internal:8789`;
với máy khác dùng IP truy cập được. `127.0.0.1` trong container là chính container.
Thay cấu hình cần restart backend. Không có thao tác deploy hoặc đổi `.env` tự động
trong bản giải quyết conflict này.

## Kiểm chứng

```bash
bash scripts/check_local.sh
# Có Mosquitto trên máy:
bash scripts/check_local.sh --run-mqtt
```

Tests dùng model/map giả, SQLite tạm và mock transport Jetson. Kết quả benchmark
phần cứng ngày 15/10 trong tài liệu riêng thuộc revision cũ, không chứng minh
semantic/latency của runtime vừa ghép. Cần live eval riêng trước khi kết luận
chất lượng Qwen, STT, TTS và GPU scheduling trên thiết bị thật.

Kiểm tra bản giải quyết conflict `e9638cf` ← `e438407`: gate đầy đủ có
**657 Python tests pass, 8 skip tùy dependency/tùy chọn; 76 JavaScript tests pass**,
bao gồm MQTT local. Ruff và whitespace pass. Regression bổ sung kiểm tra profile
qua gateway chung, mất model vẫn giữ câu hỏi/consent, không phát lại effect,
STT health không chặn event loop và chặn cả map matrix khi offline. Chưa chạy
Jetson/GPU, model hoặc VietMap thật trong lần giải quyết conflict này.
