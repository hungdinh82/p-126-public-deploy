# Baseline và ma trận chuyển Core v2

Baseline trước sửa theo kết quả rà soát đã chốt: collect 1.220 Python tests, nhóm
trọng tâm 145 pass; frontend 3/4 file pass, confirmation fail ở assertion chữ ký.
Chưa có full-suite/live model/VietMap acceptance. Đây là bằng chứng trước cutover,
không so trực tiếp số test mới để suy ra chất lượng tăng/giảm.

| Lỗi / invariant | Kiểm chứng mới |
|---|---|
| “đi Tràng An” và “dẫn đường đến Tràng An” chọn semantic/provider khác nhau | `test_core_workflow`: hai cách nói assert cả semantic và destination model calls, cùng preview |
| Parser thay nghĩa trước/sau model | `test_model_gateway`, `test_native_tools`: model được gọi, schema repair/timeout, valid clarify không fallback |
| Fallback cắt tiền tố câu ghép | `test_core_state`, `core_cases`: offline câu ghép giữ task/question, whole yes mới decode |
| Chọn POI bị hiểu là consent start | `test_core_state`: chọn số/handle chỉ preview; `test_api_v2`: UI answer bind question |
| Trực tiếp start hoặc sửa/append task chạy vượt consent | `test_core_workflow`: action không có question không được lái; append thu hồi consent |
| Navigation và itinerary giữ bước sau khác nhau | `test_core_workflow`: nhạc chờ start cho navigate, chờ tính xong cho itinerary |
| Thông tin xen ngang làm mất navigation/nhạc | `test_core_workflow`: text và typed manual search cùng suspend/resume đúng task ID, hỏi lại consent |
| Sửa draft đè journey | `test_core_state`, `core_cases`: tạo B giữ route/journey A; từ chối B không dừng A |
| Không tìm được POI làm mất tên | `test_core_state`: bổ sung area giữ POI/draft ID và query gồm cả tên/khu vực |
| Telemetry làm cũ consent/draft | `test_core_recovery`: draft revision và dialogue checkpoint không đổi |
| P phải chờ model/map | `test_core_state`: barrier giữ dependency, P trả receipt khi barrier chưa mở; map result cũ bị fence |
| Hai tab, thứ tự snapshot/reply/control | `test_core_state`, frontend NavigationStore/gear: stale revision/sequence/epoch/cache bị chặn |
| Dispatch xong mất response/restart | `test_core_recovery`: ledger đối chiếu trước grant; park khi restart; không tự phát D |
| Effect unknown bị gửi lại | `test_core_state`: reservation chưa có receipt giữ unknown, không phát effect |
| Stream lặp command hoặc lệch HTTP | `test_api_v2`: cùng ID/payload chỉ một effect, payload khác 409; cancel trước dispatch không chạy model result đến muộn |
| Geometry lặp trong checkpoint | `test_core_state`: calculation bất biến/reference, checkpoint không có geometry |
| Nâng cấp làm mất history/memory/ledger | `test_core_recovery`: backup/reset một lần, giữ effect/memory, vô hiệu grant cũ |
| Charging/khứ hồi/pause/resume | Component suites energy/calculation/charging và core scenario hai chặng; live/hardware riêng |

Test cũ giữ nguyên hoặc đổi vị trí được ánh xạ trong
`eval/legacy/test_migration.json`. Test dựa vào grammar/state v1 được ghi từng ca
trong `eval/legacy/retired.json`, cùng snapshot nguồn. Những tên replacement ghi
nhóm invariant chung, không phải chứng nhận từng ca đã có bản tương đương.
Các runner cũ chứa parser model stub đã lưu lịch sử; runner mới dùng authored
semantic và oracle độc lập. Những chỗ label ngôn ngữ, browser/network thực và
phần cứng chưa kiểm không được tính là pass bằng test fixture.
