# Đa bước, chọn địa điểm bằng lời và hồ sơ VF8 Plus — 10/10/2026

Đọc read-only các bản ghi 925–958 trong `data/vivi_rag.sqlite3`: 34 lượt,
20 lượt Google thật và 14 typed UI events. Không sửa history/memory/ledger.
20 lượt model được lưu vào `eval/audit_925_958.jsonl` để replay output lỗi cũ.

## Lỗi và nguyên nhân

| Log | Biểu hiện | Sửa |
|---|---|---|
| 925 | Chào + nóng quá bị nuốt thành câu chào | Lexer tách tiền tố chào; prompt giữ yêu cầu. Guard sửa request/reply bỏ mất một command đã phân tích đủ, không đổi clarification/unsupported hợp lệ |
| 928 | Không thích dùng điều hoà lại đặt 24°C | Phủ định sở thích không cấp quyền thực thi. Không giả lập tắt điều hoà bằng thay nhiệt độ; capability power chưa có |
| 930–931 | Parser offline thiếu sửa hai cửa trước/sau | Contextual slot grammar hỗ trợ cặp vị trí, giữ mức mở và task |
| 935 | Query chứa cả “tới” | Destination grammar nhận “tới/đến”, không gửi từ nối vào POI query |
| 942–943 | Chuỗi mới bị append sau tuyến cũ; khởi hành tuyến cũ trước | Prompt phân biệt request mới và sửa/thêm task. Guard chỉ sửa thứ tự theo grammar compound đã hiểu đủ |
| 946–950 | Tìm + đặt nhiệt độ + dẫn đường còn hai bước; UI chọn POI làm mất bước nhiệt độ | Tách ba bước, thêm `use_search_results` cho plan. Reducer coi UI/voice selection là slot answer của plan đang chờ, giữ task/step IDs |
| 947 | Nút bắt đầu làm mất task đang chờ chọn địa điểm | Trả hỏi chọn trước, không thay task |
| 958 | Từ chối capo rồi yêu cầu đóng cốp bị coi toàn câu chưa hiểu | Refusal + thao tác mới hoàn chỉnh thay pending cũ. Đóng cốp vẫn phải qua xác nhận theo policy R2; không mở capo |

## Luồng dẫn đường

Tìm → nêu tối đa ba kết quả bằng lời → người dùng chọn → tính tuyến/pin → mở
map xem trước → xác nhận khởi hành → màn hình chạy. Kể cả chỉ có một kết quả
vẫn chờ chọn; “đồng ý” khi chọn một địa điểm chỉ chọn địa điểm, chưa khởi hành.
Khi nhiều kết quả thì hỏi thứ tự, không tự chọn. Bản đồ đang chạy được giữ trong
khi tìm đích mới; đổi tuyến vẫn cần xác nhận riêng.

GPS/config được lấy mà chưa mở map. Câu hỏi chọn sau khi có vị trí đi qua hàng
đợi âm thanh chung. Vị trí fallback được nói rõ là mô phỏng. Các kiểm tra
revision/event sequence và consent còn nguyên.

`navigation.plan` có query hoặc `use_search_results=true`. Chuỗi search → climate
→ plan không tìm lại POI và không bỏ mất climate. Selection sửa đúng bước đang
chờ, không phát lại bước hoàn tất. Task contract 8 vô hiệu grant cũ; preview
auto-select của contract cũ phải chọn lại. Completed effects/command IDs giữ nguyên.
Semantic prompt: `semantic-unified-9`. Trace bổ sung mã/path lỗi schema, không lưu
raw output lỗi hoặc prompt vào trường debug đó.

## Thông số và dữ liệu còn thiếu

- `data/energy/vf8-plus.json`: mặc định backend, VF8 Plus bản pin 87,7 kWh/DC
  149 kW theo [VinFast](https://vinfastauto.com/vn_vi/dat-coc-xe-vf8). Tiêu hao
  22 kWh/100 km, đường cong sạc và connector là giả định mô phỏng. Người dùng đã
  chấp nhận thông số gần đúng; `simulated=true`, provenance từng trường trong file.
- `data/energy/charging-stations.json`: kho trạm thật đang rỗng.
- `data/energy/charging-stations.simulated.json`: một điểm tổng hợp để thử tính
  sạc, không xác nhận trạm V-Green thực. Bật riêng qua CHARGING_STATIONS_PATH.
- `data/energy/README.md`: schema nhập JSON/CSV, cách cấu hình và giới hạn dữ liệu.

Kiểm chứng tay: 120 km × 22/100 × 1,15 = 30,36 kWh = 34,62 điểm SOC. Xuất
phát 70% đến còn 35,38%; xuất phát 40% không đạt dự phòng 15%, cần sạc.
Hai kịch bản từ file này đã kiểm thử plan → chọn → xác nhận → running.

Để đưa vào hành trình thực cần kho trạm xác minh (toạ độ, đầu sạc, công suất,
SOC tối đa, khả năng tiếp cận, nguồn/ngày), GPS/SOC telemetry thật và hiệu chỉnh
tiêu hao/charge curve. Chưa có dữ liệu trụ trống và hàng chờ; UI giữ “chưa biết”.
Không suy công suất từ tên POI. Road route tới trạm vẫn bắt buộc; hồ sơ xe đủ
không có nghĩa tự sinh được kho trạm hoặc kế hoạch sạc khả thi.

## Kiểm thử

Regression mới replay parser + Google/local mock; UI/voice selection giữ bước
còn lại; early start; refusal; hai mức pin; migration và idempotency. Google/local
mock kiểm tra contract/reducer, không đo chất lượng model local thật.

Parser: 4/4 episode, 14/14 lượt, 3 clarification, 0 unexpected executions theo
oracle; p50/p95 225,34/439,99 ms trên máy này, dùng DB tạm và fixture.
Kết quả sau guard, development regression, không phải bộ đánh giá độc lập.

Giữ `navigation-voice-oct10-google-initial.json`: 3/4 episode, Google phục vụ
6 lượt (4 đạt), parser fallback phục vụ 8 lượt (8 đạt); một schema failure sau
repair mở circuit breaker. Ca lời chào bị model trả reply đã có regression bổ sung.
Các kết quả cuối được ghi trong artifact parser/Google tương ứng.

Lượt Google cuối đạt **4/4 episode, 14/14 lượt sau fallback**, cụ thể:

| Tầng thực sự phục vụ | Lượt đạt | Hỏi lại | p50 / p95 | Unexpected executions theo oracle |
|---|---:|---:|---|---:|
| Google | 9/9 | 2 | 3128,15 / 6401,70 ms | 0 |
| Parser trong lượt Google | 5/5 | 1 | 224,21 / 7786,03 ms | 0 |

Một câu “đồng ý” bị Google trả semantic không qua validator cả sau schema
repair (root `value_error`). Resolver chuyển parser rồi mở circuit breaker;
không bỏ validator để chấp nhận proposal sai. p95 fallback có cả thời gian chờ
Google lỗi. Vì vậy không gọi kết quả này là 14/14 lượt Google thật. Model local
thật chưa được đo, local mock chỉ xác minh reducer/contract.

Bốn file regression frontend pass, gồm case GPS không mở map và đợi chọn địa
điểm; Ruff và `git diff --check` pass. Tests không dùng xe hoặc trạm thật.
Full suite trên code cuối: **493 passed in 219,39s**, gồm API, parser,
confirmation, memory, navigation/energy, restart/replay và MQTT broker localhost.

Chưa restart/deploy backend; reload để nhận prompt 9, contract 8 và hồ sơ mới.
Frontend cần refresh để nhận luồng lấy GPS/chọn địa điểm trước khi mở map.
