# Chẩn đoán local model Jetson — 17/10/2026

Đã giảm đáng kể đầu vào model và bổ sung khả năng quan sát lỗi. **Chưa đạt độ đúng tổng quát**: evaluator chạy thật qua `SemanticResolver` đạt 15/26 ca phát triển. Các ca phủ định và tính năng không hỗ trợ vẫn có thể bị diễn giải thành thao tác được hỗ trợ. Không coi bản sửa là nghiệm thu điều khiển xe.

## Log hiện tại và nguyên nhân

Đọc `data/2026-10-17/turns.jsonl` bằng truy cập chỉ đọc. Hai lượt lúc 15:09:53 và 15:10:32 giờ Việt Nam:

| Câu nói | Kết quả cuối | Thời gian model | Input / output tokens |
|---|---|---:|---:|
| bật nhạc | clarify: hoàn tất địa điểm/xem tuyến trước khi khởi hành | 27,896 ms | 11,654 / 83 |
| tăng điều hoà lên 25 độ | cùng lời nhắc về tuyến | 15,044 ms | 11,750 / 84 |

Cả hai có `finish_reason=tool_calls`, một call, không repair; không phải lỗi cắt JSON hoặc timeout. Nội dung phản hồi khớp nhánh bảo vệ `navigation.start` khi thiếu câu hỏi `approve_execution` trong `validate_semantic`. Đây là suy luận từ validator và kết quả cuối, vì log cũ không giữ tên capability đề xuất trước khi bị chặn.

Các vấn đề quan sát được:

1. Native tool schema trước sửa dài 22,538 ký tự, lặp nhiều envelope/slot giữa 30 capability. Chat template đưa schema vào đầu vào model. Probe tổng hợp với phiên trống đã dùng khoảng 8,995–9,002 token, chưa cần lịch sử thật.
2. Context điều hướng chứa đồng thời draft/journey, các biểu diễn lặp của selected/candidates/offer và kết quả tính toán. Trong snapshot request có sẵn, phần navigation riêng khoảng 9,000 ký tự sau khi đã bỏ geometry. Hai log mới lưu workspace khoảng 242,000 ký tự; đây là kích thước log/checkpoint, không phải toàn bộ đều được gửi model.
3. Lịch sử cũ và tuyến chưa hoàn tất dễ lấn át lệnh độc lập. Probe tổng hợp xác nhận native hiện tại sai cả ở phiên trống: nhạc/điều hòa thành conversation, web hoặc reply. Vì vậy context cũ không giải thích hết lỗi.
4. Wire contract cho phép model sinh `answer`, `modify` và ID không tồn tại ngay cả khi phiên không có câu hỏi/tác vụ tương ứng. Server chặn một số lỗi nhưng log cuối làm mất đề xuất ban đầu.
5. Schema đúng không chứng minh nghĩa đúng. Thử shared argument schema, bỏ kiểu tham số, thay sampling theo hướng dẫn Qwen và thêm một bước phân loại đều chưa đủ tin cậy; không đưa các biến thể đó vào implementation.
6. Evaluator cũ tạo `ModelProvider` với budget mặc định 10 giây thay vì cấu hình production 30 giây cho local, và luôn hash prompt cloud dù đang đo local. Điều này làm việc so sánh/chẩn đoán sai lệch.

## Thay đổi

- `local_semantic_transport=json_schema`: một đề xuất SemanticTurn mỗi request, dùng grammar của llama.cpp qua `response_format`; schema vẫn được gửi tới server nhưng không còn lặp trong chat template. Giữ `native` để so sánh hoặc dùng runtime local khác. Không áp dụng `strict_schema` làm mọi slot bắt buộc.
- Catalog đáng tin cậy nằm trong system prompt; câu hiện tại đứng cuối user prompt dưới nhãn riêng. Prompt phân biệt nhiệt độ đích/độ thay đổi, vị trí chưa rõ và thao tác mới với context cũ.
- Giới hạn prose lịch sử còn bốn lượt, tối đa 400 ký tự mỗi trường. Giữ pending question, bước có thể sửa, tham số, destination/provenance, ID/revision và memory; projection navigation giữ lifecycle/tham chiếu/choices, bỏ tính toán và grant/binding. Không thay đổi checkpoint.
- Schema lấy capability từ catalog; bỏ `answer` khi không có pending question, cố định question_id khi có; chỉ cho task handles thực; bỏ start khi chưa có câu hỏi approve_execution. Chỉ các capability có vị trí cabin mới mang metadata loại trừ/tham chiếu cabin.
- Dùng chung danh sách semantic slots giữa prompt/schema và `ActionDraft` validator. Kiểm tra domain, reducer, consent và cấm thực thi output bị cắt vẫn được giữ.
- Log thêm proposed act/capabilities, kết quả validator tham chiếu, cached tokens và thời gian prefill/decode. `accepted` của validator không có nghĩa model hiểu đúng câu nói.
- Evaluator dùng budget production, nhận fixture session/history, hỗ trợ repeats, ghi đúng prompt/schema hash và đo số lượng/giá trị tham số cùng hành vi chặn toàn bộ lệnh.

Tài liệu llama.cpp xác nhận `response_format` hỗ trợ schema-constrained JSON và thông tin cache/timing; Jinja ở phiên bản được pin đã bật mặc định, nên **không kết luận thiếu `--jinja` là nguyên nhân**: [server API b11447](https://github.com/ggml-org/llama.cpp/blob/b11447/tools/server/README.md). Qwen có hướng dẫn sampling riêng; thử sampling không tự giải quyết corpus này: [model card chính thức](https://huggingface.co/Qwen/Qwen3-1.7B).

## Kiểm chứng

Artifact: [kết quả evaluator](local-model-runtime-2026-10-17-results.json). Corpus: [26 ca tổng hợp](../../eval/local_semantic_context_cases.jsonl), 13 câu ở phiên trống và 13 câu khi có draft/journey/lịch sử giả. Không có map/xe thật; không dùng memory hoặc hội thoại thật làm đầu vào suy luận.

Chạy lại:

```bash
TTS_PROVIDER=off .venv/bin/python -m eval.semantic_benchmark \
  --provider local --dataset eval/local_semantic_context_cases.jsonl \
  --output /tmp/local-semantic.json --repeats 1
```

Kết quả một lượt mỗi ca, model `vivi-qwen3-1.7b`:

- Input khoảng **2,836–3,062 token** thay vì khoảng 9,000–9,500 ở bốn probe native tổng hợp trước sửa. Không so sánh thời gian với log thật như một benchmark được kiểm soát.
- p50 model **1,591 ms**, p95 **2,506 ms**, chậm nhất **4,221 ms**. Phần lớn request có prefix cache; đây không phải p95 cold start hay toàn bộ voice pipeline.
- **15/26 đúng đủ oracle; 0 fallback, 0 repair, 0 truncated output.** Chấm cả act, số action, slot, vị trí chưa rõ và việc chặn lệnh; không tính JSON hợp lệ là đúng.
- Điều hòa, kính, sưởi ghế, thiếu vị trí và compound điều hòa+kính đã qua cả hai context. Nhạc vẫn lỗi theo context: có khi reply, có khi bịa media_query từ lệnh chung. Thời tiết thiếu query/location hoặc lặp action; điều hướng vẫn nhầm capability/slot. Các ca phủ định/không hỗ trợ vẫn không chặn đúng.

Corpus được dùng phát triển sau khi quan sát lỗi, không phải held-out. Chưa đo speech load đồng thời, hoạt động xe, hội thoại thật nhiều lượt hoặc phân bố latency cold/warm nhiều mẫu. Cần đạt các nhóm lỗi còn lại trên corpus độc lập trước khi coi local model đủ tin cậy. Không thể kết luận tăng timeout hoặc chỉ đổi grammar sẽ sửa khả năng hiểu nghĩa của model 1.7B.

## Phạm vi và giới hạn truy cập

Không restart/deploy backend hoặc sửa process/model trên Jetson. Automatic approval review chặn replay có lịch sử/memory/navigation thật vì egress dữ liệu nhạy cảm; đã tiếp tục bằng fixture tổng hợp. SSH đọc log server không thành công vì thiếu xác thực, nên không khẳng định startup flags thực tế khác với source. Không gọi cloud provider, không thực thi capability và không cập nhật phiên người dùng trong quá trình kiểm chứng.

Kiểm tra phần mềm: **662 passed, 21 skipped** (`pytest tests`, 35.90 s); Ruff và `git diff --check` qua. Các bài test dùng worker thread treo lúc shutdown trong sandbox; một API test pass ngoài sandbox, sau đó toàn bộ suite được chạy ngoài sandbox với fixture DB tạm và chặn mạng ngoài loopback như cấu hình test hiện có. Việc test phần mềm qua không thay thế nghiệm thu chất lượng model nêu trên.
