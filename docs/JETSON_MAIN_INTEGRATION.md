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
export LLM_PROVIDER=local
export LOCAL_LLM_MODEL=vivi-qwen3-1.7b
export LOCAL_LLM_BASE_URL=http://<jetson-private-ip>:8789/v1
export LOCAL_LLM_API_KEY='<token trong service.env>'
export JETSON_AI_URL=http://<jetson-private-ip>:8789
export JETSON_AI_TOKEN="$LOCAL_LLM_API_KEY"
export JETSON_AI_POLICY=prefer_local
.venv/bin/python -m uvicorn src.vivi.api.app:app --host 127.0.0.1 --port 8787
```

Cloud fallback dùng API key/provider đã cấu hình trên Laptop. Dùng
`JETSON_AI_POLICY=offline_only` nếu không cho phép cloud. Không commit token.
STT/TTS trên public Laptop API vẫn dùng adapter/config hiện có; remote speech
adapter chưa thuộc bản ghép này. Jetson `/test` kiểm thử trực tiếp cả ba model.

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
