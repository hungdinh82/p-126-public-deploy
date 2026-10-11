# Nghiệm thu ViVi unified-4 — 09/10/2026

## Phạm vi đã triển khai

Sửa parser/prompt và clarification checkpoint; task/grant TTL độc lập; memory
có bằng chứng; capability corrections/offer/multi-step. Thêm workflow navigation
với typed UI events, chọn POI đúng loại, road routing/preview/consent, pending
route khi đang chạy, ledger và revision/sequence guards. Thêm bộ tính EV, nguồn
hồ sơ/kho trạm JSON/CSV, curve charging và alert/audio coordination.

Không đổi greeting; không tải Qwen, không restart/deploy backend đang chạy.
Một worker. [Thiết kế, cấu hình và migration](../../docs/navigation_energy.md).

## Suite và kiểm chứng tích hợp

- Full Python suite với broker MQTT local: **432 passed, không skip**.
- Sau bổ sung chọn phương án sạc: navigation + semantic/fallback regression cuối **75 passed**, gồm 22 ca navigation/core mới và regression guard/context..
- MQTT riêng: **13/13**; API + STT/TTS transport smoke riêng: **27/27**.
- Frontend: **4 file test pass**, gồm consent/typed controls/barge-in, route cũ,
  sequence cũ, completion và audio priority. Ba ca mới được chạy riêng qua Node
  VM, đều pass. Đây là DOM/map/audio mock, chưa phải browser + thiết bị âm thanh.
- Voice development regression: **57/57** intent/route; memory **30/30**.
- Ruff và `git diff --check` pass. Không có import v1, shadow v1 hoặc branch
  điều khiển graph bằng câu trả lời tiếng Việt. Đã xoá demo classifier/executor
  trình duyệt và navigation executor cũ không dùng.

Navigation HTTP test chạy từ câu nói → NDJSON provider/step/navigation/final →
preview → endpoint confirmation → driving → progress/pin. Schema event lỗi trả
422; thiếu origin yêu cầu vị trí; sai POI/unreachable bị loại; restart, replay,
revision cũ, huỷ pending và thay route không cấp quyền chạy tự động.
Thử riêng restart cả simulator adapter kiểm tra pin khôi phục từ checkpoint;
model bỏ sót vế điều khiển bị chặn, clarification giữ chuỗi tới lượt sửa.

Charging tests đối chiếu tay, đủ/thiếu pin Hà Nội–Hải Phòng, một/nhiều điểm dừng,
giới hạn SOC/công suất, thiếu dữ liệu, restart khi sạc và SOC UI/adapter/checkpoint.
Telemetry thấp hơn dự kiến tạo cảnh báo, giữ route và chuẩn bị đề xuất; warning
không lặp ID và critical được báo khi mức độ tăng. Consent/exclusion/revision/
idempotency trong các bộ này không có thực thi sai.

## Hội thoại: theo tầng thực sự phục vụ

Accuracy dưới đây là **kết quả workflow có semantic guard**, gồm status, intent,
exact arguments/state và điều kiện không thực thi. Không phải accuracy output
model thô; fixture development đã được dùng để sửa lỗi, không phải held-out.

| Bộ / tầng thực sự | Lượt đúng | Hoàn tất kịch bản | Hỏi lại | p50 / p95 | Thực thi ngoài yêu cầu | Guard sửa |
|---|---:|---:|---:|---:|---:|---:|
| Bộ cũ, parser | 21/21 | 9/9 | 3 | 142 / 198 ms | 0 | 0 |
| Bộ mới, parser | 17/17 | 6/6 | 7 | 232 / 313 ms | 0 | 0 |
| Bộ cũ, Google | 13/13 | 9/9 **toàn pipeline** | 4 | 2161 / 5049 ms | 0 | 4 |
| Bộ cũ, fallback parser | 8/8 | thuộc 9/9 phía trên | 0 | 143 / 10243 ms | 0 | 0 |
| Bộ mới, Google | 17/17 | 6/6 | 7 | 2837 / 5121 ms | 0 | 9 |

Bộ cũ có một cloud timeout tại budget 10 giây; circuit breaker dẫn đến 8 lượt
parser, local endpoint không kết nối được. p95 fallback bao gồm thời gian chờ
cloud, không phải latency parser thuần. Không gộp 21/21 thành Google accuracy.
Kết quả hợp lệ unsupported/clarification vẫn kết thúc nhánh model, không fallback.
Model có thể hiểu sai numeric slot/handle/unresolved; guard và reducer đã chặn
các lỗi được tái hiện. Số guard sửa còn cao: chưa có cơ sở khẳng định chất lượng
hội thoại tổng quát đã đạt production.

Các report máy đọc được:
[cũ parser](navigation-baseline-parser.json), [mới parser](navigation-dialogue-parser.json),
[cũ Google](navigation-baseline-google.json), [mới Google](navigation-dialogue-google.json),
[voice](navigation-voice-parser.json), [memory](navigation-memory-parser.json).
API evaluation dùng fixture và DB tạm trống; không gửi DB/log cá nhân lên API.

## Toàn bộ audit 854–897 và local mock

44 câu gốc trong [fixture audit](../audit_854_897.jsonl) được replay theo thứ tự.
Oracle kiểm tra không lưu tên “tôi”, không bật sưởi khi nghe ốm/sốt, offer → đồng
ý, trái/sau/cửa-kính, exclusions/huỷ, giữ chuỗi điều hoà/nhạc và delegation.

| Tầng | Oracle regression | Hỏi lại | p50 / p95 | Thực thi sai ở các ca có oracle |
|---|---:|---:|---:|---:|
| Parser | 44/44 | 15 | 120 / 193 ms | 0 |
| Cloud mock | 44/44 | 15 | 134 / 183 ms | 0 |
| Local mock | 44/44 | 15 | 136 / 194 ms | 0 |

[Artifact contract](navigation-audit-contract.json) / runner
`python -m eval.audit_contract_benchmark`. Mock phát frame parser cùng contract;
đây là phép đo chuyển trạng thái/latency với fixture, **không phải NLU accuracy
của cloud hoặc Qwen**. Qwen thật chưa chạy, nên accuracy/task completion/latency
thực của tầng local chưa có phép đo.

## VietMap thật và năng lượng

Smoke bằng key đã cấu hình, truy vấn công khai “nhà thuốc” với origin Hà Nội
`21.0287,105.8524` được ghi là fixture, không phải GPS người dùng:

- Autocomplete trả 10 kết quả; lấy chi tiết “Nhà Thuốc 167 Bạch Mai Phố Bạch Mai”.
- Matrix: **2764.99 m**; Route adapter: **2772.3 m**, **286.5 s**, **88 điểm**.
- Không khởi chạy hành trình/không gọi xe. Matrix v4 và adapter Route hiện có
  có thể khác quãng đường vì phiên bản routing engine; năng lượng dùng road Route
  thực sự hiển thị. Smoke không kiểm chứng chất lượng tile trong browser.

Mốc năng lượng đã chạy E2E **synthetic**: dung lượng 60 kWh, 20 kWh/100 km,
curve 100/40 kW, route 120 km với legs 50/70 km. SOC 70% đi thẳng; SOC 40% cần
một lần sạc khoảng 10.4 phút, SOC đến trạm 23.33%, tại đích 15%. Đây không phải
thông số VF8/trạm thật. Repo chưa có hồ sơ đúng phiên bản pin và kho trạm V-Green
đủ nguồn để nghiệm thu lộ trình sạc thực tế. Hệ thống không tự điền công suất
trạm hoặc tính phút khi thiếu dữ liệu.

## Migration và giới hạn còn lại

Runtime migration v4 chạy một lần, backup DB, giữ history/memory/ledger; revoke
grant cũ và upgrade checkpoint contract. Memory repair đã chạy riêng: xoá đúng
default/display_name/value “tôi”/source turn/revision 1, giữ mọi bản sửa khác.
Backup: `data/memory/long_term-before-name-repair-20261009T144645564128.sqlite3`.

Đã nghiệm thu core và navigation bằng API/mock + smoke VietMap thật; EV engine
được nghiệm thu trong simulation. Còn cần cấu hình dữ liệu thực có nguồn và kiểm
thử trên thiết bị: Qwen inference, GPS, browser map rendering, microphone/speaker,
pin/đường đi/trụ sạc thực tế. Không gọi tính năng lộ trình sạc thật là sẵn sàng.
