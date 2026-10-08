# Chạy ViVi với Google Gemini

Provider `google` dùng API của Google cho hiểu intent và sinh câu trả lời RAG.
Embedding E5, truy xuất handbook SQLite, history và memory vẫn chạy local.

`config.toml` hiện chọn:

```toml
llm_provider = "google"
google_model = "gemini-3.5-flash-lite"
google_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
google_max_tokens = 4096
google_reasoning_effort = "low"
rag_local_only = false
```

Đặt `GOOGLE_API_KEY` trong `.env`; cũng nhận `GEMINI_API_KEY` khi không có
`GOOGLE_API_KEY`. Không ghi key vào `config.toml`. Biến môi trường và `.env`
có ưu tiên cao hơn cấu hình TOML.

Khởi động lại backend sau khi đổi cấu hình:

```bash
.venv/bin/python run.py
```

Tải lại giao diện sau khi đổi provider mặc định trong config. Lựa chọn cũ
trong trình duyệt được bỏ khi mặc định backend thay đổi; lựa chọn chủ động
trên giao diện vẫn được giữ nếu mặc định backend không đổi.
API `/api/v1/health` báo provider, model và graph đã khởi tạo; thông tin này
không chứng minh Google đang truy cập được. Khi lỗi gọi model, graph ghi lỗi
và dùng fallback sẵn có. Chế độ Google cần Internet.

Để quay lại offline, chọn `llm_provider = "local"` với SLM đã cấu hình, hoặc
`"rules"`, rồi đặt `rag_local_only = true`. Script `run_rag_local.sh` ép chế
độ offline, vì vậy không dùng script đó để chạy Google.

Đường gọi dùng [API tương thích OpenAI của Google](https://ai.google.dev/gemini-api/docs/openai),
với endpoint và API key riêng của Google. Model mặc định theo
[tài liệu Gemini 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).

Kiểm tra hồi quy không gọi dịch vụ cloud:

```bash
LLM_PROVIDER=rules VIVI_RAG_LOCAL_ONLY=true .venv/bin/python -m pytest -q
```
