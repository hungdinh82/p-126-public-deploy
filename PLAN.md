# Kế hoạch tích hợp giọng nói và LLM cho ViVi

> Cập nhật: 2026-09-22  
> Trạng thái: Đang triển khai — Giai đoạn 1 hoàn tất; PhoWhisper và ZeroTTS đã chạy thật trên MacBook Air M1  
> Phạm vi: Tích hợp PhoWhisper medium (STT), LLM local qua API và ZeroTTS với giọng community **Mai Chi** vào prototype ViVi hiện tại.

## 1. Mục tiêu MVP

Tạo một vòng hội thoại bằng tiếng Việt có thể chạy xuyên suốt:

```text
Người dùng nói
  → thu âm / phát hiện kết thúc câu
  → PhoWhisper chuyển giọng nói thành văn bản
  → backend điều phối hội thoại
  → LLM hiểu ý định và đề xuất thao tác có cấu trúc
  → safety gateway kiểm tra
  → vehicle adapter thực hiện và xác minh trạng thái
  → ZeroTTS tổng hợp câu trả lời bằng giọng Mai Chi
  → giao diện phát âm thanh và hiển thị trạng thái
```

MVP chỉ được coi là hoàn thành khi người dùng có thể nói một lệnh, nhìn thấy transcript, nhận phản hồi bằng giọng nói, và các thao tác xe mô phỏng chỉ được báo thành công sau khi trạng thái đã được xác minh.

## 2. Nguyên tắc kiến trúc

- API key, khóa dịch vụ và logic an toàn chỉ nằm ở backend; frontend không gọi trực tiếp LLM hay TTS bằng khóa bí mật.
- STT, LLM và TTS được bọc bằng interface/adapter riêng để có thể đổi nhà cung cấp hoặc model mà không sửa luồng hội thoại.
- LLM không trực tiếp điều khiển xe. LLM chỉ sinh một yêu cầu có cấu trúc; safety gateway quyết định cho phép, hỏi lại hay từ chối.
- Phản hồi “đã thực hiện” chỉ được phát sau khi vehicle adapter trả acknowledgement và đọc lại trạng thái thành công.
- Nội dung người dùng đang nói có thể hiển thị dạng tạm thời, nhưng chỉ transcript cuối cùng mới được gửi đến LLM.
- Mọi timeout hoặc lỗi dịch vụ phải đưa hệ thống về trạng thái an toàn, không tự lặp lại lệnh điều khiển.
- Audio và transcript được lưu cục bộ vô thời hạn để đánh giá, cho đến khi người vận hành tự xóa. Dữ liệu không được commit lên Git hoặc gửi ra ngoài máy.
- Pipeline runtime phải hoạt động offline sau khi các model/dependency đã được tải và đóng gói trên máy.

## 3. Kiến trúc đề xuất

### Frontend

Giữ `index.html`, `style.css` và phần hiển thị không gian trong `app.js`. Tách logic hội thoại khỏi animation thành các module:

```text
web/
  voice/audio-capture.js       Thu âm, quyền microphone, click-to-record
  voice/audio-playback.js      Phát, dừng và ngắt TTS
  conversation/client.js       Kết nối backend, nhận event theo phiên
  conversation/store.js        State machine và dữ liệu hội thoại
  ui/orb-state.js              Ánh xạ event sang chuyển động của ViVi
```

Trong giai đoạn đầu có thể tiếp tục dùng JavaScript thuần; chưa cần thêm framework chỉ để tích hợp pipeline.

### Backend orchestration

Đề xuất tạo một service Python vì PhoWhisper thường thuận tiện triển khai trong hệ sinh thái Python. Cấu trúc mục tiêu:

```text
server/
  app.py                       HTTP/WebSocket entrypoint
  config.py                    Đọc biến môi trường và kiểm tra cấu hình
  schemas.py                   Contract request, event và tool call
  orchestrator.py              Điều phối một lượt hội thoại
  session_store.py             Trạng thái phiên ngắn hạn
  data_store.py                Lưu audio, transcript và metadata cục bộ
  adapters/
    stt/base.py
    stt/phowhisper.py
    llm/base.py
    llm/api.py
    tts/base.py
    tts/zerotts.py
    vehicle/base.py
    vehicle/simulator.py
  safety/
    policy.py
    validator.py
  tests/
```

Backend chỉ bind vào loopback (`127.0.0.1`) trong MVP đầu tiên và giao diện truy cập qua `localhost`. Sau lần setup/tải model ban đầu, PhoWhisper, LLM, ZeroTTS, dữ liệu phiên và vehicle simulator đều chạy trên cùng laptop, không phụ thuộc cloud.

### Giao thức frontend–backend

Giai đoạn đầu dùng HTTP cho bản ghi hoàn chỉnh để giảm độ phức tạp. Người dùng nhấn mic một lần để bắt đầu; hệ thống tự kết thúc khi phát hiện khoảng lặng, đồng thời cho phép nhấn lần nữa để dừng thủ công:

- `POST /api/v1/stt`: audio → transcript.
- `POST /api/v1/turn`: transcript + session context → event/action/response.
- `POST /api/v1/tts`: text + voice → audio.
- `GET /api/v1/health`: trạng thái service và các adapter đã cấu hình.

Khi pipeline ổn định, chuyển lượt hội thoại sang WebSocket để phát event thời gian thực và hỗ trợ partial transcript. Contract event dự kiến:

```json
{
  "type": "listening|transcript.partial|transcript.final|thinking|action.proposed|action.executing|action.verified|speaking|done|error",
  "session_id": "uuid",
  "turn_id": "uuid",
  "data": {}
}
```

Mỗi lượt phải có `turn_id` để chống thực hiện lặp khi client retry.

## 4. Contract giữa LLM và safety gateway

LLM phải trả structured output theo schema; không parse thao tác xe từ văn bản tự do.

```json
{
  "intent": "climate.set_temperature",
  "arguments": {
    "value_celsius": 25
  },
  "needs_clarification": false,
  "clarification_question": null,
  "spoken_response": "Mình sẽ đặt nhiệt độ ở 25 độ."
}
```

Safety gateway kiểm tra tối thiểu:

- intent có nằm trong allowlist;
- kiểu dữ liệu và giới hạn tham số;
- trạng thái xe hiện tại có cho phép thao tác;
- lệnh có mơ hồ, phủ định hay xung đột không;
- thao tác có cần xác nhận người dùng không;
- `turn_id` đã được thực thi chưa.

Các intent MVP ban đầu:

- `climate.set_temperature`
- `window.set_position`
- `media.play`
- `media.pause`
- `vehicle.get_status`
- `manual.search`
- `conversation.clarify`

## 5. State machine hội thoại

```text
idle
  → listening
  → transcribing
  → thinking
  → validating
  → executing
  → verifying
  → synthesizing
  → speaking
  → idle
```

Nhánh lỗi có thể đi từ mọi trạng thái về `recoverable_error` hoặc `blocked`, sau đó trở về `idle`. Khi người dùng nhấn mic trong lúc TTS đang phát, hệ thống phải dừng playback trước khi mở lượt mới; đây là cơ chế barge-in và sẽ được triển khai sau khi click-to-record ổn định.

## 6. Các giai đoạn thực hiện

### Giai đoạn 0 — Chốt contract và đo baseline

- Khóa PhoWhisper medium; chuẩn bị hai profile runtime: macOS Apple Silicon M1 và Windows với NVIDIA GTX 1650 4 GB VRAM.
- Đóng gói model/dependency cục bộ và kiểm tra khởi động khi máy không có mạng.
- Dùng package `zerotts`, model `zeroweight-ai/ZeroTTS` và pack community `voices/VIVI.zip` (voice ID `VIVI`, tên hiển thị “Mai Chi”); xác minh license/phạm vi dùng giọng trước khi vượt khỏi demo.
- Định nghĩa interface LLM độc lập với nhà cung cấp.
- Lập bộ 30–50 câu lệnh tiếng Việt có giọng vùng miền, từ đệm, phủ định và câu mơ hồ.
- Ghi baseline độ trễ cho từng chặng.

**Điều kiện hoàn tất:** contract request/response được thống nhất và có fixture test đại diện.

### Giai đoạn 1 — Backend skeleton và simulator

- Khởi tạo Python service, config từ `.env`, health check và structured logging.
- Tạo interface cho STT/LLM/TTS/vehicle cùng fake adapters.
- Chuyển bộ luật hiện có trong `resolveCommand()` sang safety gateway phía server.
- Thêm schema validation, `session_id`, `turn_id`, timeout và idempotency.
- Viết unit test cho allowlist, phủ định, giới hạn nhiệt độ và policy khi đang lái.

**Điều kiện hoàn tất:** frontend có thể chạy trọn pipeline bằng fake adapters, không chứa secret.

### Giai đoạn 2 — PhoWhisper STT

- Thu âm mono trên frontend bằng click-to-record; tự kết thúc theo khoảng lặng và có nút dừng thủ công.
- Chuyển audio về format đầu vào thống nhất ở backend, đồng thời lưu bản chuẩn hóa gắn với `turn_id`.
- Tích hợp PhoWhisper medium; warm-up model khi khởi động.
- Trên macOS M1 ưu tiên backend tương thích Apple Silicon sau benchmark, có CPU fallback. Trên Windows benchmark CUDA với GTX 1650 4 GB; nếu không đủ VRAM hoặc không ổn định thì dùng CPU/offload fallback, không coi GPU là yêu cầu bắt buộc.
- Thêm giới hạn thời lượng/kích thước, timeout, xử lý audio rỗng và transcript confidence thấp.
- Hiển thị transcript cuối cùng để người dùng biết ViVi đã nghe gì.
- Đánh giá WER và task success trên bộ câu lệnh MVP, ưu tiên đúng thực thể như nhiệt độ và vị trí cửa.

**Điều kiện hoàn tất:** câu nói tiếng Việt đi từ mic đến transcript ổn định trên thiết bị mục tiêu.

### Giai đoạn 3 — LLM API và tool contract

- Cài adapter LLM với base URL, model name và API key qua biến môi trường.
- Yêu cầu structured output theo JSON Schema; từ chối output không hợp lệ.
- Giới hạn context, timeout, retry có kiểm soát và circuit breaker.
- Tách system prompt thành file có version; thêm test prompt regression.
- Chỉ đưa action đã qua safety gateway vào vehicle simulator.

**Điều kiện hoàn tất:** LLM xử lý đúng bộ lệnh đánh giá và không thể vượt qua allowlist/tool schema.

### Giai đoạn 4 — ZeroTTS, giọng Mai Chi

- Tích hợp local package `zerotts` với model `zeroweight-ai/ZeroTTS`.
- Khóa voice mặc định là `VIVI`, nạp từ `voices/VIVI.zip` (tên hiển thị “Mai Chi”).
- Dùng streaming synthesis; adapter nhận các chunk mono `float32` 48 kHz rồi truyền/phát theo contract nội bộ.
- Chuẩn hóa văn bản nói: số, đơn vị, ký hiệu và câu quá dài.
- Hỗ trợ hủy request/playback khi người dùng tạo lượt mới.
- Cache các câu hệ thống ngắn, không chứa dữ liệu riêng tư, để giảm độ trễ.
- Fallback sang phản hồi chữ khi TTS lỗi; không fallback im lặng sang giọng khác.
- Xác nhận quyền sử dụng giọng Mai Chi cho phạm vi sản phẩm; model card hiện mô tả các giọng đóng gói cho đánh giá/demo.

**Điều kiện hoàn tất:** phản hồi hợp lệ phát đúng giọng, có thể ngắt, và lỗi TTS không làm mất kết quả dạng chữ.

### Giai đoạn 5 — Tích hợp UI và tối ưu trải nghiệm

- Ánh xạ state backend vào chuyển động của orb thay cho timer giả hiện tại.
- Thay nút mic demo bằng click-to-record thật; hiển thị rõ đang thu, đang chờ khoảng lặng và nút dừng.
- Đồng bộ transcript, action proposal, kết quả verify và audio playback.
- Giảm chuyển động khi lái; mọi lỗi có thông báo ngắn, dễ hiểu.
- Thêm barge-in sau khi luồng cơ bản đạt độ ổn định.

**Điều kiện hoàn tất:** người dùng hoàn thành các kịch bản MVP không cần bàn phím.

### Giai đoạn 6 — Kiểm thử và đóng gói demo

- Unit test adapter, schema, safety policy và state reducer.
- Integration test với fake services và contract test với dịch vụ thật.
- Test audio nhiễu, im lặng, mất mạng, API chậm, output LLM sai schema và người dùng nói chồng TTS.
- Test toàn bộ pipeline khi ngắt mạng sau khi đã tải sẵn model.
- Đo p50/p95 cho STT, LLM, TTS và tổng thời gian đến âm thanh đầu tiên.
- Viết hướng dẫn cấu hình, khởi động, vị trí dữ liệu được lưu, dung lượng đã dùng, export và xóa thủ công.

**Điều kiện hoàn tất:** chạy được bằng một quy trình được ghi trong README, toàn bộ test bắt buộc đạt, và không có khóa bí mật trong repo/browser bundle.

## 7. Mục tiêu chất lượng ban đầu

Các ngưỡng sau là mục tiêu để đo, chưa phải cam kết cuối cùng:

- Tỷ lệ hoàn thành đúng tác vụ trên tập lệnh MVP: ≥ 90%.
- Không có thao tác ngoài allowlist hoặc chưa qua safety gateway: 100%.
- Giá trị số quan trọng (nhiệt độ, phần trăm cửa) phải được xác nhận hoặc hỏi lại khi confidence thấp.
- p95 từ lúc kết thúc câu nói đến lúc bắt đầu phát TTS: mục tiêu ≤ 3 giây trên môi trường demo; tách số đo theo từng adapter.
- Khi một adapter lỗi, UI trở về trạng thái có thể thử lại và không thực hiện lệnh hai lần.

## 8. Cấu hình dự kiến

Tên biến môi trường sẽ được cố định khi có tài liệu API thực tế:

```dotenv
STT_PROVIDER=phowhisper
PHOWHISPER_MODEL=vinai/PhoWhisper-medium
PHOWHISPER_DEVICE=<auto|cpu|cuda|mps>

LLM_BASE_URL=<local-endpoint-chua-chot>
LLM_API_KEY=<optional-local-secret>
LLM_MODEL=<chua-chot>

TTS_PROVIDER=zerotts
ZEROTTS_MODEL=zeroweight-ai/ZeroTTS
ZEROTTS_DEVICE=cpu
ZEROTTS_VOICE=VIVI

STORE_AUDIO=true
STORE_TRANSCRIPTS=true
DATA_DIR=./data
DATA_RETENTION_DAYS=0
```

`DATA_RETENTION_DAYS=0` nghĩa là không tự động xóa. File `.env`, thư mục `data/`, model cache và file audio phải nằm trong `.gitignore`; repo chỉ cung cấp `.env.example` không chứa secret hoặc dữ liệu người dùng. Metadata tối thiểu của một lượt gồm `session_id`, `turn_id`, thời gian, đường dẫn audio, transcript STT, intent/action, kết quả verify, lỗi và latency từng chặng.

Tài liệu model tham chiếu:

- [vinai/PhoWhisper-medium](https://huggingface.co/vinai/PhoWhisper-medium)
- [zeroweight-ai/ZeroTTS](https://huggingface.co/zeroweight-ai/ZeroTTS)

## 9. Thông tin được hoãn đến giai đoạn LLM

Đã chốt ba adapter LLM: OpenAI Responses API, Google Gemini API và local OpenAI-compatible API. Model cụ thể vẫn được cấu hình qua biến môi trường; mặc định dùng `rules` để chạy và test offline khi chưa chọn model. Không còn câu hỏi chặn Giai đoạn 1.
