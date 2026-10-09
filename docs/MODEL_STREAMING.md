# Tùy chọn streaming cho AI server

Nhánh: `feat/model-streaming`. Streaming là tùy chọn theo request; các API
JSON/WAV hiện có vẫn hoạt động khi không bật streaming. Áp dụng cho gateway AI
trên PC và Xavier, không thay workflow, RAG hoặc vehicle modules trên Laptop.

## Chạy và chọn chế độ

Trên PC đã setup:

```bash
bash scripts/ai_server.sh run
```

Mở `http://127.0.0.1:8789/test`. Chọn riêng **SLM streaming**, **TTS streaming**,
**STT streaming**. Các checkbox mặc định tắt. Bật STT streaming để micro gửi
audio khi đang nói; bấm **Dừng** để kết thúc lời nói và nhận final. Upload audio
cũng có thể gửi thành các chunk. Transcript tạm được cập nhật trong ô nhập.
Bật TTS streaming để browser phát PCM ngay khi nhận được; vẫn có WAV để nghe
lại sau khi hoàn tất. Nút **Dừng** ngắt HTTP stream/phát audio đang chạy.

Giữ `ai_server_auth_required` giống nhau ở server và backend. Nếu tắt auth,
không cần token. Nếu bật, HTTP dùng Bearer token; browser WebSocket gửi token
trong frame JSON đầu tiên, không đặt token vào query string hay log.

### Xavier: dependency WebSocket

Lock Jetson thêm **wsproto 1.2.0** (pure Python); các package đã pin khác giữ
nguyên. Vì manifest kiểm tra hash lock, sau khi cập nhật code hãy chạy:

```bash
bash scripts/jetson_ai.sh setup --profile agx
bash scripts/jetson_ai.sh run
```

Setup dùng lại model/build cache và runtime CUDA/ORT hiện có; không cần xóa
build, tải lại model hoặc đổi OS/JetPack. Chờ setup hoàn tất thành công.
Streaming không cần đổi phiên bản llama.cpp, Whisper hoặc ZeroTTS.

## SLM: SSE

API OpenAI compatible:

```bash
curl --no-buffer http://127.0.0.1:8789/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"2 cộng 3 bằng bao nhiêu?"}],"max_tokens":64,"stream":true}'
```

Response `text/event-stream`: `data: {choices:[{delta:...}]}` và kết thúc bằng
`data: [DONE]`. Dùng `stream: false` hoặc bỏ trường này để lấy JSON như trước.
Tool-call delta cũng được chuyển tiếp; chỉ dùng arguments khi đã nhận đủ và
validate JSON, không thực thi tool từ arguments chưa hoàn chỉnh.

API envelope ViVi cũng nhận `stream: true` ở ngoài hoặc trong `input`:

```json
{
  "contract_version": "1.0",
  "capability": "llm",
  "request_id": "req-1",
  "call_id": "call-1",
  "attempt": 1,
  "timeout_ms": 60000,
  "stream": true,
  "input": {
    "messages": [{"role": "user", "content": "Xin chào"}],
    "max_tokens": 128
  }
}
```

Gửi tới `POST /v1/llm/generate`. SSE có `event: delta`, cuối cùng `event: done`
với toàn bộ `result.text`, `result.message`, latency và `first_chunk_ms`.
`request_id`, `call_id`, `attempt` được giữ trên mỗi event. Khi lỗi giữa stream,
nhận `event: error`, không có `done` thành công. Stream OpenAI dùng JSON error
trong `data:`; client phải kiểm tra error, không chỉ kiểm tra `[DONE]`.

## TTS: PCM từng đoạn

```bash
curl --no-buffer http://127.0.0.1:8789/v1/tts/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text":"Xin chào, tôi là ViVi.","stream":true,"format":"pcm_s16le"}' \
  -o /tmp/vivi-stream.pcm
```

`POST /v1/tts/stream` là alias bật streaming mặc định. Response là raw PCM16
little endian, mono, **không có WAV header**. Đọc `X-Audio-Sample-Rate`,
`X-Audio-Channels`, `X-Audio-Format` và `X-ViVi-First-Chunk-Ms` từ headers.
Không đổi đuôi `.pcm` thành `.wav`; client cần bọc WAV hoặc phát PCM đúng format.
HTTP chunk có thể chia giữa một sample 2 byte, client cần giữ byte lẻ để nối
với chunk tiếp theo. ZeroTTS sinh audio từng decoder chunk; không đợi sinh xong
rồi chia file thành các phần.

Bỏ `stream` hoặc đặt `false` ở `/v1/tts/synthesize` để lấy WAV như trước.
Lỗi trước audio đầu tiên trả JSON với HTTP lỗi. Lỗi sau headers làm ngắt
audio stream; không chèn JSON vào PCM và không tự chạy lại synthesis.

## STT: WebSocket

Kết nối `ws://<AI-server>:8789/v1/stt/stream` (`wss://` khi qua HTTPS).
Frame đầu tiên là JSON:

```json
{"type":"start","sample_rate":16000,"format":"pcm_s16le","channels":1}
```

Nếu bật auth, thêm `token` hoặc dùng header Authorization từ client hỗ trợ.
Sau event `ready`, gửi các binary frame chứa PCM16 mono 16 kHz (không WAV
header), tối đa 64 KB/frame, tổng tối đa 60 giây. Mỗi frame có `ack` để client
giới hạn số frame đang chờ. Sau khi gửi xong, gửi text frame `stop`.

Các event:

- `ready`: socket có thể nhận PCM.
- `partial`: transcript hiện tại và số `revision`; thay thế toàn bộ partial cũ.
- `ack`: số byte server đã nhận.
- `done`: transcript cuối, latency và thời gian update đầu tiên.
- `error`: code/message, socket đóng.

Whisper hiện tại là engine nhận dạng theo đoạn. Gateway nhận audio liên tục và
nhận dạng lại utterance đang tích lũy mỗi khoảng một giây audio; partial có thể
đổi từ/ngữ nghĩa. Chưa có VAD tự kết thúc, transcript đã chốt từng token hoặc
decoder giữ cache xuyên các chunk. Client gửi `stop` để nhận final; chỉ final
mới dùng làm input cho workflow/điều khiển xe. Transformer Whisper xử lý mọi
window 30 giây để không bỏ phần cuối của utterance dài hơn 30 giây.
Một socket có deadline 90 giây; số update thực tế phụ thuộc tốc độ inference.
`POST /v1/stt/transcriptions` upload WAV vẫn hoạt động như trước.

## Trạng thái, ngắt kết nối và giới hạn

`GET /v1/capabilities` báo `stream`, `stream_protocol`, `stream_mode` (speech),
`first_chunk_ms`, `last_latency_ms`, trạng thái CPU/GPU thực tế. Không gọi đó là
thời gian phát ở loa: `first_chunk_ms` là thời gian có phần dữ liệu đầu tiên
trên server; UI cũng đo thời gian đến client.

Các model dùng chung giới hạn concurrency GPU. Request đang bận trả lỗi/busy;
STT giữ slot trong mỗi lần decode, TTS/SLM giữ slot trong request đang chạy.
Khi client ngắt, upstream được đóng; native inference step đang chạy được chờ
hoàn tất trước khi nhả slot, tránh hai request cùng đụng model. SLM/TTS có
deadline theo config server. Không retry stream sau khi đã phát dữ liệu.

Backend Laptop vẫn dùng response hoàn chỉnh để validate semantic/tool JSON.
Remote STT/TTS chưa nối vào public API app; các lựa chọn này thuộc AI gateway
và giao diện `/test`. Không tự đổi luồng voice hay modules của các thành viên khác.

## Kiểm tra thực tế trên PC

RTX 3050 Ti Laptop GPU 4 GB, ba model đều `ready` và sử dụng GPU. Một lần smoke:

| Model | Phần đầu | Tổng | Kết quả |
| --- | --- | --- | --- |
| SLM | ~2.98 s | ~3.44 s | 11 SSE frames, trả `2 cộng 3 bằng 5.` |
| TTS | ~0.67 s | ~2.09 s | 5 chunk PCM, 153600 byte, 48 kHz |
| STT | ~1.38 s phía server | ~1.50 s | Partial rồi final `Xin chào, tôi là Vivi.` |

Ngắt stream SLM rồi gọi non-stream tiếp trả HTTP 200. Đây là smoke một prompt,
không phải benchmark/đánh giá độ chính xác chung. Chưa chạy bản streaming trên
Xavier. Đã kiểm thử checkbox SLM/TTS/STT, upload audio và phát PCM trong Chromium
headless. Microphone giả chạy qua AudioWorklet, nút Dừng giải phóng các media
track; chưa kiểm thử microphone/loa vật lý hoặc chất lượng speech trong môi
trường sử dụng thực tế.

## File liên quan

- `jetson_ai/stream_routes.py`: proxy SLM SSE, PCM TTS và WebSocket STT.
- `jetson_ai/streaming.py`: SSE encoding, worker và cleanup khi bị cancel.
- `jetson_ai/python_llm.py`: streaming bridge llama-cpp-python trên PC.
- `jetson_ai/speech.py`: ZeroTTS decoder chunks, Whisper windows.
- `jetson_ai/static/test.html`, `streaming.js`: checkbox và client streaming.
- `tests/test_model_streaming.py`, `tests/test_ai_streaming_frontend.cjs`: protocol,
  cancellation và parsing mạng bị chia nhỏ.

Tham khảo backend: [llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
và [ZeroTTS streaming](https://github.com/zeroweight-ai/ZeroTTS#usage).
