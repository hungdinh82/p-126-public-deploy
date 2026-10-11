# Chạy ViVi trên máy tính cá nhân

Hướng dẫn chạy app bình thường trên Linux/Ubuntu hoặc Ubuntu trong WSL.
AI server và backend chính chạy trong hai terminal tại cùng checkout project.
Không cần Jetson hoặc Docker. AI server mặc định dùng GPU NVIDIA.

## 1. Cấu hình

Trong `config.toml`:

```toml
llm_provider = "local"
local_llm_model = "vivi-qwen3-1.7b"
ai_server_url = "http://127.0.0.1:8789"
ai_server_auth_required = false
jetson_ai_policy = "prefer_local"
```

Không cần đặt `AI_SERVER_TOKEN` trong `.env`. Các biến môi trường và `.env`
ưu tiên hơn `config.toml`; cập nhật hoặc xóa override provider, URL hay xác thực
cũ nếu muốn dùng cấu hình trên.

Nếu AI server ở máy khác, đổi `ai_server_url` thành URL truy cập được và đặt
`ai_server_auth_required = false` trong cấu hình của cả hai máy. Khi không yêu
cầu token, mọi client truy cập được địa chỉ server đều có thể gọi model.

## 2. Chuẩn bị một lần

Mở terminal trong repo. Đường dẫn trên máy hiện tại:

```bash
cd /home/kah/vinai/P-126
bash scripts/ai_server.sh setup
```

Máy hiện tại đã có dependency/model nên có thể bỏ qua bước này.
Máy mới cần `uv` và GPU NVIDIA hoạt động trong Linux/WSL. Setup chuẩn bị môi
trường Python, dependency và tải model; lần đầu cần Internet.
Chỉ chuyển sang bước chạy khi setup hoàn tất thành công.

## 3. Terminal thứ nhất: chạy AI server

```bash
cd /home/kah/vinai/P-126
bash scripts/ai_server.sh run
```

Giữ terminal này chạy. Service nạp SLM Qwen3-1.7B, STT Whisper và TTS ZeroTTS,
phục vụ API tại `http://127.0.0.1:8789`. SLM nạp trước; speech nạp ở nền.
Lệnh `run` không chạy setup hoặc tải model tự động.

Xem trạng thái từng model, thiết bị GPU và thời gian xử lý tại:

```text
http://127.0.0.1:8789/test
```

Trang này theo dõi và gọi trực tiếp model; không chạy workflow/RAG/xe của app.
Log service nằm trong `data/ai-server/`.

Có thể bật checkbox **SLM streaming**, **TTS streaming**, **STT streaming**
riêng cho mỗi model trong trang này. Chi tiết protocol, giới hạn Whisper và
cập nhật dependency Jetson ở [MODEL_STREAMING.md](MODEL_STREAMING.md).

Các tùy chọn khi cần:

```bash
bash scripts/ai_server.sh run --port 8790
bash scripts/ai_server.sh run --device cpu
bash scripts/ai_server.sh run --gpu-layers 12
```

Nếu đổi cổng, cập nhật `ai_server_url` tương ứng trước khi chạy backend.
Muốn cho máy khác kết nối, dùng `--host <IP-LAN>` hoặc `--host 0.0.0.0`;
backend ở máy khác phải dùng IP/hostname thực tế của AI server.

## 4. Terminal thứ hai: chạy app chính

```bash
cd /home/kah/vinai/P-126
.venv/bin/python run.py
```

Mở trên trình duyệt:

```text
http://127.0.0.1:8787
```

Backend chạy workflow `unified-3`, RAG, session/history và vehicle gateway.
AI server thực hiện inference SLM theo URL đã cấu hình. Mặc định cấu hình hiện
tại dùng xe mô phỏng memory; cấu hình MQTT vẫn hoạt động theo module hiện có.

Giọng nói của app chính vẫn dùng adapter STT/TTS trong cấu hình backend.
Remote STT/TTS của AI server chưa được nối vào public API của app chính.
Nếu chỉ muốn nhập bằng chữ và tránh nạp speech ở backend, chạy:

```bash
STT_PROVIDER=off TTS_PROVIDER=off .venv/bin/python run.py
```

Đây vẫn là app chính; hai biến trên chỉ tắt speech cho lần chạy đó.

## 5. Sử dụng và kiểm tra

Trong app, thử:

- `Đặt điều hòa 24 độ` — xem trạng thái xe mô phỏng thay đổi.
- `Kết nối cáp sạc AC thế nào?` — kiểm tra câu trả lời cẩm nang và nguồn.
- `Mở cửa bên tài` — xác nhận khi được yêu cầu; thao tác vẫn chịu kiểm tra an toàn.

Trạng thái backend:

```text
http://127.0.0.1:8787/api/v1/health
```

Kiểm tra model ở gateway chuyển sang `ready` và provider local khả dụng trong
health. Với `prefer_local`, nếu AI server không sẵn sàng, backend có thể dùng
cloud đã cấu hình hoặc rules. Muốn tránh gọi cloud, đặt
`jetson_ai_policy = "offline_only"` rồi restart backend.

## 6. Dừng và chạy lại

Nhấn **Ctrl+C** ở từng terminal. AI server dừng các model process do nó tạo;
backend dừng riêng. Lần tiếp theo chỉ cần chạy lại hai lệnh `run` và `run.py`,
không cần setup lại nếu môi trường/model không thay đổi.

## Kiểm tra đã thực hiện trên máy hiện tại

Entry point `ai_server.sh run` đã được kiểm tra trên RTX 3050 Ti Laptop GPU:
SLM/STT/TTS đều báo `ready` và sử dụng GPU; gọi chat API không token trả HTTP 200
và SLM trả lời đúng câu `2 cộng 3 bằng bao nhiêu?`. Service đã dừng sau kiểm tra.
Kết quả này xác nhận service khởi động và chat API hoạt động; chưa xác nhận
độ chính xác mọi tác vụ hay chất lượng STT/TTS qua suy luận thực tế ở lần này.
