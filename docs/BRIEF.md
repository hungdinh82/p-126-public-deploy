# VIVI Cabin Copilot — One-page Brief

| Thuộc tính | Nội dung |
|---|---|
| Mã đề | DEV-01 |
| Nhóm | P-126 |
| Người dùng chính | Tài xế xe điện VinFast (mô phỏng) |
| Người dùng phụ | Kỹ sư hệ thống theo dõi chất lượng và vận hành |
| Giai đoạn | Gate G1 — chốt bài toán và thiết kế |

## Bối cảnh và pain point

Trợ lý trong xe phụ thuộc cloud có thể chậm hoặc mất một phần năng lực trong hầm, tầng hầm và khu vực mạng yếu. Trong khi lái xe, người dùng không nên phải nhìn và thao tác màn hình nhiều lần. Tuy nhiên, đưa LLM xuống thiết bị edge tạo ra ba vấn đề mới: tài nguyên hạn chế, chất lượng tiếng Việt/tool calling không ổn định và rủi ro thực hiện sai thao tác vật lý. Câu trả lời kỹ thuật về xe còn phải dựa đúng sổ tay, đúng dòng xe và có nguồn kiểm chứng.

## Problem statement

Tài xế cần một trợ lý cabin vẫn xử lý được các tác vụ thiết yếu khi mất mạng, phản hồi đủ nhanh để sử dụng rảnh tay, trả lời câu hỏi về xe có căn cứ và không cho phép mô hình ngôn ngữ tự quyết định những thao tác nhạy cảm.

## Giải pháp đề xuất

Xây dựng **VIVI Cabin Copilot**, một prototype offline-first chạy trên edge:

- Nhận lệnh tiếng Việt qua voice hoặc text.
- Dùng SLM lượng tử hóa để phân loại, lập kế hoạch giới hạn và tạo phản hồi.
- Dùng RAG local để trả lời sổ tay kèm page/section citation; thiếu bằng chứng thì abstain.
- Dùng typed tools và MQTT để điều khiển vehicle digital twin, không kết nối xe thật.
- Dùng deterministic safety policy R0-R3; lệnh cửa/kính cần HITL một lần, có thời hạn.
- Ghi trace từng bước để đo latency, intent quality, grounding và tool success.

## Giá trị khác biệt

ViVi hiện hữu đã có nhiều năng lực điều khiển bằng giọng nói. Đề tài không nhằm xây lại toàn bộ ViVi, mà tập trung chứng minh bốn khoảng trống kỹ thuật có thể đo được: **offline SLM, grounded handbook RAG, deterministic safety/HITL và edge performance evidence**.

## MVP và non-goals

**MVP:** hai vai trò; text/voice; tối thiểu năm intent cabin; MQTT simulator; RAG citation/abstention; HITL cửa/kính; local Q4; lỗi có giải thích; eval và latency trace.

**Không làm:** CAN/xe thật; phanh, lái hoặc truyền động; automotive safety certification; bản đồ/POI đầy đủ; OTA thật; voice biometrics; camera pipeline phức tạp.

## Chỉ số thành công dự kiến

| Chỉ số | Mục tiêu prototype |
|---|---|
| Intent macro F1 | ≥ 0,90 trên bộ test tiếng Việt được version hóa |
| Tool completion | ≥ 95% với simulator acknowledgement |
| Citation precision | ≥ 0,95 |
| Unsupported-answer rate | ≤ 0,05 |
| Lệnh R2 | 100% có xác nhận hợp lệ; timeout/deny không thực thi |
| Offline core | 100% core regression chạy khi chặn WAN |
| Voice latency | p95 ≤ 2,5 giây trên phần cứng được công bố, hoặc có phân tích nguyên nhân |

Các mục tiêu chỉ có ý nghĩa khi báo cáo model, quantization, phần cứng, sample size, warm/cold state và điều kiện tiếng ồn.

## Rủi ro và cách giới hạn

- **Scope năm tuần:** bảo vệ P0; multi-agent nâng cao, OTA và UI polish là P1/P2.
- **Edge không đủ tài nguyên:** benchmark ngay tuần 1; giảm model/context và giữ text fallback.
- **STT tiếng Việt trong cabin:** dùng noisy test set, hiển thị transcript và hỏi lại khi confidence thấp.
- **RAG bịa:** filter theo model/version, threshold, citation validation và abstention.
- **LLM gọi tool sai:** typed schema, allowlist, deterministic policy và simulator acknowledgement.

## Kết quả bàn giao

Một Dockerized prototype, tài liệu kiến trúc, bộ eval có phiên bản, báo cáo metric và demo tối đa năm phút cho thấy: ngắt WAN vẫn điều khiển cabin mô phỏng, RAG trả lời có nguồn và lệnh nhạy cảm không thể vượt HITL.
