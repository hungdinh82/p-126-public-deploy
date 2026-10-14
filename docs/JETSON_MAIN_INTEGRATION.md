# Ghép main và Jetson AI — 10/10/2026

Main được lấy từ origin ở commit `3e3977e`. Các thay đổi được ghép vào nhánh
`feat/jetson-ai-pipeline`; workspace hiện có merge commit `3e39cb1`.
Các điều chỉnh integration bổ sung được chia commit riêng cho routing/fallback,
lint test và hướng dẫn vận hành.

## Luồng chạy

```text
Browser → Laptop /api/v1 → một workflow unified-3
                           ├─ session/checkpoint, history, memory
                           ├─ handbook SQLite/E5/FTS5, evidence/citation
                           ├─ policy/confirmation → vehicle/MQTT + effect ledger
                           └─ inference → Jetson hoặc cloud hoặc rules
```

Jetson giữ private gateway và ba engine STT/SLM/TTS, không nhận quyền điều khiển
xe hoặc giữ session/RAG. Workflow mới của main được giữ; không phục hồi graph cũ.
UI, vehicle simulator, vehicle gateway, persistence và memory lấy nguyên thay đổi
main. Hai lựa chọn cục bộ trong config.toml (`phowhisper`, `zerotts`) được phục hồi
ở working tree; index chỉ chứa các bổ sung config của main.

## Điểm ghép

- Provider aliases trong một graph không đồng nghĩa model đã cấu hình. Router
  dùng `semantic_providers` để chọn đúng backend khi Jetson không sẵn sàng.
- Khi policy bật và provider local lỗi, resolver thử cloud đã cấu hình rồi rules.
  `offline_only`/local-only không có cloud trong chuỗi. Policy `disabled` giữ
  thứ tự resolver của main. Quyền dùng cloud không tự cấp API key.
- Bước sinh handbook answer theo cùng chuỗi fallback; evidence luôn truy xuất
  trên Laptop. Lỗi generation không chạy lại bước vehicle đã thực thi.
- Session/checkpoint, deadline, cancellation, circuit breaker và effect ledger
  của main được giữ; provider thay đổi không tạo workflow/session khác.
- RAG hợp nhất adapter fallback, có log khi embedding lỗi; lỗi SQLite không bị
  bắt chung như lỗi model. Giữ cả các regression tests của main và feature.
- Health hiển thị policy offline và chuỗi fallback phù hợp với composition.

## Chạy

### Cấu hình một địa chỉ AI server

Chạy app bình thường trên PC, trong hai terminal tại repo:

```bash
# Chuẩn bị AI runtime một lần nếu máy chưa có dependency/model.
bash scripts/ai_server.sh setup
# Terminal 1: giữ STT/SLM/TTS và gateway chạy cho app.
bash scripts/ai_server.sh run
# Terminal 2: backend chính, workflow/session/RAG và giao diện 8787.
.venv/bin/python run.py
```

PC `run` dùng GPU mặc định, cổng 8789; không tạo hội thoại test hoặc đổi backend
sang profile test. Có thể chọn `--host`, `--port`, `--device cpu`, `--gpu-layers`.
Log nằm trong `data/ai-server/`. Dừng bằng Ctrl+C; một engine dừng sẽ dừng cả
service thay vì để app tiếp tục gọi process hỏng. Các model speech nạp ở nền;
xem trạng thái tại gateway `/test`. Nếu thiếu model/runtime, chạy `setup` trước.
Trên Xavier, cùng launcher gọi script Jetson native, dùng lại môi trường đã setup.
Remote STT/TTS đã được ghép vào app Laptop. Soniox/VieNeu là tầng Cloud;
Whisper/ZeroTTS trên Jetson là tầng offline; browser voice là TTS cuối cùng.

Gateway AI có thể chạy trên Jetson, PC hoặc máy khác có runtime phù hợp.
Laptop chỉ cần địa chỉ truy cập được; không phụ thuộc SSH alias hay loại máy.
Trong `config.toml`:

```toml
llm_provider = "google"
local_llm_model = "vivi-qwen3-1.7b"
ai_server_url = "http://192.168.55.1:8789"
ai_server_auth_required = true
jetson_ai_policy = "allow_cloud"
```

Trong xe luôn giữ `ai_server_auth_required = true`. Trong `.env` của Laptop đặt
`AI_SERVER_TOKEN=<token từ /mnt/jetson-data/vivi-ai/service.env trên Jetson>`.
Backend tự dùng cùng URL và token cho capability probe và SLM; tự nối `/v1`.
Biến môi trường ưu tiên hơn TOML. Xóa/cập nhật các override `LLM_PROVIDER`,
`AI_SERVER_URL` hoặc `JETSON_AI_URL` cũ nếu muốn dùng cấu hình trong file.
`JETSON_AI_TOKEN` và `LOCAL_LLM_API_KEY` cũ vẫn dùng được.

Nếu backend chạy native cùng máy AI server, dùng `http://127.0.0.1:8789`.
Nếu chạy Docker Desktop và AI server ở máy host, dùng
`http://host.docker.internal:8789`; với Jetson/máy khác, dùng IP LAN/Tailscale
mà script `run` in ra. `127.0.0.1` trong container chỉ đến chính container.
Gateway phải lắng nghe trên địa chỉ truy cập đó; trên Jetson LAN dùng
`JETSON_AI_BIND=<IP-LAN> bash scripts/jetson_ai.sh run`.
Thay URL cần restart backend; dùng Docker thì chạy
`docker compose up -d --build backend` để đưa TOML mới vào image.

Các endpoint capability của gateway ViVi vẫn cần thiết khi policy được bật.
Với LM Studio/llama.cpp độc lập, đặt `jetson_ai_policy = "disabled"` và cấu hình
`local_llm_base_url`, model, API key trực tiếp như trước.
Phần cấu hình này dùng chung capability probe và adapter LLM/STT/TTS.

Sau khi bạn tự commit/push bản merge, cập nhật checkout Laptop/Jetson bằng nhánh
feature. Nếu có local edits, sao lưu/ghép trước; không reset chúng để pull.

Jetson, từ repo:

```bash
bash scripts/jetson_ai.sh setup --profile agx
# Chỉ chạy khi setup báo hoàn tất, bao gồm smoke thoát sạch.
bash scripts/jetson_ai.sh run
```

Laptop cần dependency mới của main, trong venv hiện có:

```bash
uv pip install --python .venv/bin/python -r requirements.txt
export AI_SERVER_URL=http://192.168.55.1:8789
export AI_SERVER_TOKEN='<token trong service.env>'
export AI_SERVER_AUTH_REQUIRED=true
.venv/bin/python -m uvicorn src.vivi.api.app:app --host 127.0.0.1 --port 8787
```

Cloud dùng API key/provider đã cấu hình trên Laptop. Connectivity Supervisor tự
chuyển Cloud → Jetson → Rules và không bao giờ tự dùng IP Tailscale. Không commit
token. Jetson `/test` vẫn kiểm thử trực tiếp cả ba model.

Smoke text, history và xe mô phỏng riêng, không sửa config cá nhân:

```bash
.venv/bin/python scripts/test_pipeline.py --mode rules
.venv/bin/python scripts/test_pipeline.py --mode jetson --policy prefer_local \
  --jetson-url http://<jetson-private-ip>:8789
```

## Kiểm tra và giới hạn

- Toàn bộ suite Python đã qua 434 tests. Sau đó bổ sung test tool-call mới và chạy
  riêng 5 test integration: gateway có auth/schema, local → cloud, offline,
  generation fallback và tool một lần đều qua.
- Ruff qua toàn bộ src/vehicle_simulator/tests; 5 test frontend qua; app.js syntax qua.
- Smoke CLI với handbook thật: đặt 25 độ verified; mở kính driver yêu cầu xác nhận;
  approve verified; hỏi kết nối sạc AC trả handbook answer có nguồn.
- Test cloud/Jetson của lần merge dùng doubles. Chưa xác nhận accuracy của SLM thật
  với semantic schema mới trên Xavier hoặc wheel native sau CPU-ID bounds fix.
  Kết quả STT Whisper-base trước đó còn sai nhiều từ tiếng Việt.

Hướng dẫn native setup và lỗi ORT ở [jetson_ai.md](jetson_ai.md).
