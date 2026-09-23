# Nhật ký quyết định kiến trúc — ViVi

> Cập nhật: 2026-09-22  
> Quy ước trạng thái: `Accepted`, `Proposed`, `Superseded` hoặc `Rejected`.

Tài liệu này ghi lại “vì sao” của các lựa chọn quan trọng. Một quyết định `Proposed` chưa được xem là cam kết triển khai.

## ADR-001 — Dùng backend orchestration làm ranh giới tin cậy

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Prototype hiện chạy hoàn toàn trong trình duyệt. Tích hợp LLM/TTS qua API sẽ cần secret và logic kiểm soát thao tác xe.
- **Quyết định:** Frontend chỉ thu/phát audio, hiển thị trạng thái và gửi yêu cầu. Backend giữ credential, điều phối STT/LLM/TTS, kiểm tra policy và gọi vehicle adapter.
- **Hệ quả:** Cần thêm service backend và cơ chế cấu hình/deploy; đổi lại secret và safety logic không bị đưa vào browser bundle.

## ADR-002 — Mỗi dịch vụ AI nằm sau một adapter độc lập

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** LLM chưa được chọn; PhoWhisper và zeroTTS có thể thay đổi cách triển khai.
- **Quyết định:** Định nghĩa interface riêng cho STT, LLM và TTS. Orchestrator chỉ phụ thuộc interface, không phụ thuộc SDK cụ thể.
- **Hệ quả:** Có thêm lớp abstraction và contract test, nhưng có thể benchmark/đổi model mà không sửa luồng nghiệp vụ.

## ADR-003 — Dùng PhoWhisper medium trên laptop local

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Sản phẩm ưu tiên tiếng Việt và người dùng đề xuất PhoWhisper.
- **Quyết định:** Dùng `vinai/PhoWhisper-medium`, chạy local trên MacBook Air M1 và máy Windows với NVIDIA GTX 1650 4 GB VRAM. MVP xử lý từng bản ghi hoàn chỉnh; adapter có profile thiết bị riêng và CPU/offload fallback.
- **Chưa quyết định:** Runtime/precision tối ưu cho từng hệ điều hành; quyết định này dựa trên benchmark thực tế.
- **Tiêu chí chấp nhận:** Độ chính xác intent/entity, p95 latency, RAM/VRAM và độ ổn định trên thiết bị mục tiêu.
- **Hệ quả:** Không khóa toàn hệ thống vào PhoWhisper; nếu benchmark không đạt, adapter cho phép thử STT khác.
- **Tham chiếu:** [Model card PhoWhisper-medium](https://huggingface.co/vinai/PhoWhisper-medium).

## ADR-004 — LLM chỉ sinh structured action, không trực tiếp điều khiển xe

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Văn bản tự do từ LLM không đủ tin cậy cho thao tác có ảnh hưởng đến trạng thái xe.
- **Quyết định:** LLM trả JSON theo schema. Safety gateway validate intent/arguments/context; vehicle adapter là thành phần duy nhất được phép thay đổi trạng thái xe.
- **Hệ quả:** Model được chọn sau phải hỗ trợ structured output đủ ổn định hoặc adapter phải có bước validate/retry. Output sai schema bị từ chối an toàn.

## ADR-005 — Chưa khóa model LLM, nhưng runtime phải local/offline

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Người dùng sẽ định nghĩa LLM sau.
- **Quyết định:** Hỗ trợ ba provider: OpenAI Responses API, Google Gemini API và local OpenAI-compatible API. Cấu hình chọn provider/model qua `.env`; `rules` là fallback offline mặc định. Chế độ offline dùng local/rules, còn OpenAI/Google là tùy chọn online.
- **Hệ quả:** Business logic không gắn với model. Chưa thể integration-test provider online khi chưa có key; local provider cần endpoint và model cụ thể.

## ADR-006 — Dùng ZeroTTS local với giọng Mai Chi

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Người dùng chọn model `zeroweight-ai/ZeroTTS` và đổi bản sắc giọng nói sang giọng community “Mai Chi”. Model hỗ trợ local CPU inference và streaming.
- **Quyết định:** Tích hợp package `zerotts` local, model `zeroweight-ai/ZeroTTS`, pack community `voices/VIVI.zip` với voice ID `VIVI` (tên hiển thị “Mai Chi”); xuất audio mono 48 kHz. Khi TTS lỗi, giữ phản hồi chữ và không tự chuyển sang giọng khác.
- **Chưa quyết định:** Quyền sử dụng giọng Mai Chi trong sản phẩm ngoài demo/evaluation.
- **Hệ quả:** Không cần TTS cloud hay API key. Model/dependency phải được tải trước để runtime offline; cần kiểm tra quyền sử dụng voice trước production.
- **Tham chiếu:** [Model card ZeroTTS](https://huggingface.co/zeroweight-ai/ZeroTTS).

## ADR-007 — Click-to-record và HTTP bản ghi hoàn chỉnh trước

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Streaming đồng thời audio, partial transcript và TTS làm tăng đáng kể độ phức tạp trong khi pipeline chưa được kiểm chứng.
- **Quyết định:** Người dùng nhấn mic một lần để bắt đầu, không cần giữ; hệ thống tự kết thúc theo khoảng lặng và cho phép nhấn lần nữa để dừng. MVP gửi bản ghi hoàn chỉnh qua HTTP. Sau khi đạt baseline, bổ sung WebSocket/partial STT nếu cần; TTS có thể streaming ngay từ adapter local.
- **Hệ quả:** Bản đầu có độ trễ cảm nhận cao hơn nhưng dễ test, trace và cô lập lỗi.

## ADR-008 — Python cho backend AI orchestration local

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** PhoWhisper và công cụ xử lý audio/model thường có tích hợp Python trực tiếp.
- **Quyết định:** Dùng Python cho service backend chạy trên cùng laptop; giữ frontend JavaScript thuần trong giai đoạn MVP. Service chỉ bind `127.0.0.1`, giao diện truy cập qua `localhost`, và phải chạy được khi offline sau bước cài đặt model.
- **Hệ quả:** Repo trở thành ứng dụng hai runtime. Cần chốt framework web và packaging khi bắt đầu Giai đoạn 1.

## ADR-009 — Lưu audio và transcript cục bộ

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** Nhóm muốn lưu dữ liệu để đánh giá STT và luồng hội thoại; audio/transcript có thể chứa thông tin riêng tư.
- **Quyết định:** Bật lưu audio và transcript ở local, không tự hết hạn; người vận hành tự xóa. Mỗi bản ghi gắn `session_id`/`turn_id` và metadata latency/action/result. Thư mục dữ liệu bị loại khỏi Git và không tự đồng bộ ra cloud.
- **Chưa quyết định:** Cơ chế consent cho người dùng thật, mã hóa at rest và định dạng export.
- **Hệ quả:** Có thể tái hiện và chấm lỗi STT tốt hơn, nhưng dung lượng sẽ tăng không giới hạn. Hệ thống phải hiển thị dung lượng và tài liệu hóa vị trí dữ liệu/cách xóa thủ công trước khi thu dữ liệu ngoài nhóm phát triển.

## ADR-010 — Xác minh trạng thái trước khi xác nhận thành công

- **Trạng thái:** Accepted
- **Ngày:** 2026-09-22
- **Bối cảnh:** API chấp nhận lệnh không có nghĩa xe đã đạt trạng thái mong muốn.
- **Quyết định:** Sau thao tác, vehicle adapter đọc lại trạng thái hoặc nhận acknowledgement đáng tin cậy. Chỉ khi verify thành công ViVi mới nói “đã”.
- **Hệ quả:** Tăng một bước và độ trễ nhưng tránh phản hồi sai. Timeout verify trả lời rằng chưa thể xác nhận, không giả định thành công.

## Các quyết định còn chờ

| ID tạm | Cần quyết định | Dữ liệu cần có |
|---|---|---|
| TBD-01 | PhoWhisper runtime/precision theo máy | Benchmark accuracy/latency/RAM/VRAM trên GTX 1650 4 GB và M1 |
| TBD-02 | Quyền sử dụng giọng Mai Chi ngoài demo | Điều khoản/license hoặc xác nhận từ đơn vị phát hành |
| TBD-03 | Nhà cung cấp/model LLM local | Endpoint, auth, structured output, streaming, tài nguyên máy |
| TBD-04 | Framework/package backend | Ràng buộc deploy và kinh nghiệm vận hành của nhóm |
| TBD-05 | Chính sách dữ liệu người dùng thật | Consent, mã hóa at rest và định dạng export |
