# Kết quả kiểm chứng Core v2

Ngày chạy: 2026-10-15 UTC. Source là working tree trên revision nền
`5a549456b678d24ef5d68550d26ddb954b7c34b1`, chưa commit; report ghi rõ dirty và hash.
Môi trường: Linux/WSL, Python 3.12.12, Node 20; DB, vehicle fixture và broker MQTT cô lập.
Contract/vận hành: [langgraph_core_v2.md](../langgraph_core_v2.md).
Baseline và nơi kiểm invariant: [core_v2_baseline.md](core_v2_baseline.md).

| Kiểm tra | Kết quả | Evidence |
|---|---|---|
| `bash scripts/check_local.sh --run-mqtt` | Pass | Ruff, whitespace, JS syntax/behavior, toàn bộ Python và Mosquitto cục bộ |
| Python | 617 pass, 7 skip | Những ca tùy chọn engine/hardware chưa có môi trường tương ứng |
| JavaScript | 76 pass | Bao gồm binding question, stale snapshots, stream/audio, gear/store |
| Workflow | 30/30 episode | [core-v2.json](../eval/results/core-v2.json), 10 scenario × 3, model/map/xe giả |
| P/D backend | 600 mẫu, đạt mọi ngưỡng | [controls-v2.json](../eval/results/controls-v2.json), 100 mẫu mỗi số/tải |

| Tải | p95 P | p95 D | Ngưỡng mỗi số |
|---|---:|---:|---:|
| Bình thường | 84,83 ms | 91,67 ms | 300 ms |
| Model đang chờ | 82,21 ms | 82,52 ms | 500 ms |
| Map đang chờ | 67,36 ms | 67,52 ms | 500 ms |

Phép đo P/D bao gồm orchestrator, domain, SQLite, receipt; không gồm HTTP/browser,
MQTT thật với xe, VietMap thật hoặc phần cứng Jetson. Dependency giữ bằng barrier
trong budget thật; mỗi batch có 10 mẫu mỗi số và warmup, khởi tạo journey riêng.

## Semantic thật

Provider Google, model đang cấu hình `gemini-3.5-flash-lite`. Không gọi capability
hoặc map; mọi câu bắt đầu từ context rỗng. Không có fallback trong cả ba đợt.

| Đợt | Kết quả | Xử lý sau đo |
|---|---|---|
| [8 câu đầu](../eval/results/semantic-holdout-v2.json) | 7/8 | Phát hiện thiếu purpose; thêm guard làm rõ chuỗi nhiều bước nếu model bỏ purpose |
| [6 câu tiếp](../eval/results/semantic-holdout-after-fix-v2.json) | 4/6 | Phát hiện bỏ bước nhạc phụ thuộc; bổ sung chỉ dẫn giữ đủ bước chờ prerequisite |
| [6 câu mới cuối](../eval/results/semantic-dependencies-v2.json) | 5/6 | Giữ nguyên điểm, chưa dùng sửa prompt |

Mỗi bộ là câu mới tại thời điểm chạy. Hai bộ đầu sau đó trở thành development
data; điểm cũ và prompt/hash vẫn giữ, không gộp thành accuracy của bản cuối.
Các ca do người phát triển viết, số mẫu nhỏ, chưa có review nhãn độc lập.

**Hạn chế còn biết:** `deps-new-5` (lập phương án đi Pleiku, tính tuyến xong phát
nhạc) được model phân loại `navigate` thay vì `itinerary`. Server thực thi dependency
theo semantic nhận được, nên ca này có thể chờ consent start thay vì chạy nhạc sau
tính toán. Guard chặn thiếu purpose; nó không phát hiện được mọi purpose có giá trị
hợp lệ nhưng hiểu sai. Chưa coi chất lượng semantic tổng quát đạt nghiệm thu.

## Phạm vi chuyển đổi

Đã chuyển runtime/API/UI trong repo, bỏ grammar production và capability trip.*,
đổi namespace checkpoint, thêm migration có backup và giữ ledger. Tests đã kiểm
reset một lần, park mô phỏng, memory/ledger qua restart, crash sau effect, stale
reply/control, unknown effect, hỏi xen ngang và chống chạy start thiếu consent.
Fallback phân biệt dấu tiếng Việt ở `đúng`/`dừng`/`đừng`; `dung` khi có câu hỏi
được yêu cầu nói rõ, không tự cấp consent.

Scenario/runner v1 được giữ trong `eval/legacy/` với manifest. Không tính chúng là
pass mới hoặc suy ra coverage tương đương suite cũ. Chưa kiểm live VietMap, xe,
Jetson và image Docker. Docker CLI trong WSL báo distro chưa được tích hợp Docker
Desktop, nên chưa chạy build/container smoke. Không chạy migration trên DB vận hành
hoặc publish/deploy trong đợt này; workflow CI đã đặt gate trước publish/deploy.
