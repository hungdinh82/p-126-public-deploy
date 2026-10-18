# VIVI Cabin Copilot — Kiến trúc hệ thống

> **Mã đề:** DEV-01 · **Nhóm:** The Odysseans (P-126)
> **Trạng thái:** Kiến trúc hiện hành — Laptop orchestrator điều phối Jetson (local) + Cloud (online).

## Sơ đồ tương tác

Mở file dưới đây trong trình duyệt để xem sơ đồ đầy đủ (có 4 chế độ: bình thường, mất Jetson, mất Internet, mất cả hai):

👉 **[docs/architecture.html](architecture.html)** — Sơ đồ kiến trúc tương tác (click để chuyển chế độ, phím P để trình chiếu).

![Architecture overview](architecture.html)

## Tóm tắt kiến trúc

**Laptop giữ Orchestrator chính.** Jetson AGX Xavier cung cấp dịch vụ local (STT, SLM, TTS, RAG, local tools). Cloud cung cấp dịch vụ online (Cloud STT/LLM/TTS, web search). Orchestrator chạy bằng code — không cần một LLM thứ hai để chọn tuyến xử lý.

### Phân vai

| Thành phần | Vai trò | Chạy ở |
|------------|---------|--------|
| **UI + Input** | Nhận text/microphone, hiển thị trạng thái kết nối | Laptop |
| **Session / Context** | Quản lý lịch sử, request ID, state | Laptop |
| **Orchestrator** | Điều phối STT → retrieval → LLM/tools → TTS; timeout, retry, fallback theo policy; health monitor tích hợp | Laptop |
| **Retrieval Lite** | Index/tài liệu/cache local chuẩn bị trước, dùng khi Jetson chết | Laptop |
| **Local STT/SLM/TTS** | Chạy offline khi mất Internet | Jetson AGX Xavier |
| **RAG local** | Index + retrieval cẩm nang | Jetson |
| **Local tools** | Thiết bị / API nội bộ | Jetson |
| **Cloud STT/LLM/TTS** | Chất lượng cao khi có mạng | Cloud |
| **Cloud tools** | Web search, dịch vụ bên ngoài | Cloud |

### 4 chế độ hoạt động

1. **Jetson + Internet** (bình thường) — Orchestrator ưu tiên Cloud, dùng RAG + local tools Jetson khi cần.
2. **Mất Jetson, còn Internet** — Cloud STT/LLM/TTS, retrieval dùng cache local trên laptop.
3. **Mất Internet, còn Jetson qua LAN** — Local STT/SLM/TTS trên Jetson, RAG local; không gọi Internet.
4. **Mất cả hai** — Chuyển sang text-only, truy xuất local, trả lời theo luật/mẫu (không có LLM).

### Nguyên tắc thiết kế

1. **Offline first** — mọi tác vụ cabin thiết yếu phải chạy ngay cả khi WAN chết. Jetson giữ full voice-to-response path.
2. **Safety before execution** — policy deterministic + HITL (human-in-the-loop) đứng giữa agent planning và mọi thao tác nhạy cảm (mở cửa, điều khiển vật lý).
3. **Grounded answers** — câu trả lời handbook phải có citation từ SQLite FTS5, hoặc explicitly abstain.
4. **Observable latency** — mỗi stage emit timing từ end-of-speech đến first audible response (xem engineer dashboard).

### Điều kiện fallback hoạt động

- **Mất Internet** phải còn LAN tới Jetson.
- **Model local** phải cài trước (SLM quantized + ZeroTTS INT8).
- **Retrieval Lite** cần index chuẩn bị sẵn trên laptop.
- **Jetson chết** → thiết bị/công cụ chỉ truy cập qua Jetson sẽ không khả dụng.

## Tài liệu liên quan

- [docs/langgraph_core_v2.md](langgraph_core_v2.md) — Contract LangGraph hiện hành
- [docs/langgraph_unified_runtime.md](langgraph_unified_runtime.md) — Runtime hợp nhất
- [docs/handbook_sqlite.md](handbook_sqlite.md) — Handbook RAG bằng SQLite FTS5
- [docs/setup_jetson_nano.md](setup_jetson_nano.md) — Setup Jetson
- [docs/AI_LOG_SETUP.md](AI_LOG_SETUP.md) — Observability setup
