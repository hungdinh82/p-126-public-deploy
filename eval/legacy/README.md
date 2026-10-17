# Lưu trữ trước Core v2

Thư mục này lưu grammar, provider router, scenario và runner dựa trên API/state cũ,
để truy nguyên lỗi và port kịch bản có chủ đích. Không import vào production hoặc
tests đang chạy. Không kỳ vọng runner cũ chạy với Core v2.

- `retired.json`: từng test ngừng chạy, lý do và nơi kiểm invariant thay thế.
- `test_migration.json`: đường dẫn test cũ → cấu trúc test mới.
- `scenarios/`: snapshot nguồn test lịch sử, giữ ID/log gốc.
- `workflow_cases.jsonl`, `memory_cases.jsonl`: dataset v1 chưa chuyển nguyên vẹn.
- `grammar.py`, `workflow_fixtures.py`: grammar/model stub cũ; không chứng minh model hiểu câu.

Không coi việc lưu trữ một ca là ca đó đã được port. Các nhóm consent, stale
state, idempotency, recovery, navigation và API có test Core v2 viết lại; không
thể suy ra coverage tương đương toàn bộ suite cũ từ số test pass mới. Dataset và
nghiệm thu hiện hành được mô tả tại [eval README](../README.md) và
[Core v2](../../docs/langgraph_core_v2.md). Artifact lịch sử vẫn giữ revision gốc.
