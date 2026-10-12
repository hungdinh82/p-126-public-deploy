# Kịch bản LangGraph

Nguồn chạy tự động là [workflow_cases.jsonl](workflow_cases.jsonl).
Mỗi dòng là một phiên độc lập. Oracle kiểm cả response, trạng thái vehicle adapter,
SQLite domain/receipt và effect đã thực thi. [README](README.md) mô tả cú pháp và cách chạy.

| Nhóm / ID | Thao tác | Điều kiện phải đạt |
|---|---|---|
| `front-right-slot` | Mở cửa → trước bên phải → xác nhận | Điền đúng ghế phụ, chưa mở trước consent; chỉ cửa đó mở |
| `correct-capability` | Mở cửa ghế phụ → sửa thành kính → xác nhận | Chuyển capability sang kính, cửa xe vẫn đóng |
| `half-correction` | Mở kính bên tài → một nửa → xác nhận | Kính thực tế ở 50% |
| `lower-windows` | Hạ tất cả kính → xác nhận | Cả bốn kính mở hết |
| `exclusion` | Mở tất cả → đóng trừ bên tài | Chỉ ba kính còn lại đóng; xác nhận lặp không tạo effect |
| `cooling-conflict` | “Nóng quá tăng điều hòa lên tối đa” | Hỏi lại, nhiệt độ chưa đổi |
| `pillar-handbook` | Hỏi cột A | Route handbook, không có vehicle effect |
| `no-weather-tool` | Hỏi thời tiết | Không phát lệnh xe cho câu không có tool |
| `cabin-scope-correction` | Mở tất cả cửa → chỉ cửa ra vào | Capo và cốp vẫn đóng |
| `slot-chain-cancel` | Đặt nhiệt độ → mở cửa thiếu zone → nhạc → hủy | Bước nhiệt độ đã xong; bước chờ/pending bị hủy; không bật nhạc |
| `deny-before-effect` | Mở kính → từ chối | Kính vẫn đóng |
| `driving-door-guard` | Xe đang chạy → mở cửa | Policy chặn effect mở cửa |
| `fresh-state-not-client` | Đặt 26°C → giảm 2°C | Tính từ trạng thái xe mới, kết quả 24°C |
| `ledger-replay` | Gửi lệnh → gửi lại cùng ID → đổi payload cùng ID | Chỉ một effect; collision bị từ chối |
| `memory-restart` | Nhớ tên → dựng lại runtime → hỏi tên → quên | Memory bền vững, thao tác quên có hiệu lực |
| `handbook-abstention` | Hỏi áp suất lốp với evidence rỗng | Không bịa giá trị hoặc phát vehicle effect |
| `stream-parity` | Lệnh nhiệt độ qua stream → replay | Có final, một effect, replay thống nhất kết quả |
| `aeon-ordinal` | Tìm hai POI AEON → chọn thứ hai | Đúng ID thứ hai; chọn điểm chưa làm xe chạy |
| `ordinal-out-of-range` | Danh sách hai điểm → chọn số 9 | Hỏi lại, không tự chọn điểm |
| `poi-name-reference` | Danh sách hai điểm → nói tên khớp | Chọn đúng ID, xe chưa chạy |
| `unknown-destination` | Search trả rỗng | Hỏi lại, không bịa candidate |
| `map-unavailable` | Map port báo lỗi → hỏi dẫn đường | Trả blocked, không có đích/effect chạy xe |
| `trip-accept-pause-resume-end` | Tạo draft → chọn → start → P → D → end → D | Pause/resume đúng journey; D sau end chỉ lái demo, không hồi sinh journey |
| `paused-A-draft-B` | Journey A → P → tạo draft B → D | D tiếp tục A; B vẫn là draft riêng |
| `D-keeps-unaccepted-plan` | Draft chưa nhận → D | Xe có thể demo; chưa nhận draft thành journey |
| `restart-no-auto-drive` | Start journey → đóng runtime → dựng lại từ SQLite | Phục hồi paused, không tự D |
| `lost-gear-response` | D → P → bỏ response/replay → snapshot receipt | Đối chiếu được receipt P, xe vẫn đứng yên |
| `stale-D-after-P` | P sequence 2 → D sequence 1 cùng client | D cũ blocked/superseded; không chạy lại |
| `two-tabs-stale-revision` | Tab A gửi P → tab B gửi D revision cũ | D bị stale_control; không có effect |
| `model-timeout-fallback` | Model treo quá budget → lệnh nhiệt độ | Trace ghi timeout; rules phục vụ và effect đúng |
| `invalid-json-fallback` | Model trả JSON lỗi | Fallback xác định, state đúng |
| `model-wrong-place-handle` | Dựng model output với handle bịa → nói “chọn số 1” | Reference xác định đi qua rules, không hỏi model để đảo danh sách |
| `P-during-map-read` | Giữ map bằng barrier → P → thả map | P hoàn tất khi map còn chờ; kết quả navigation cũ bị chặn |
| `P-during-model-read` | Giữ model bằng barrier → P → thả model | P hoàn tất trước model; kết quả navigation cũ bị chặn |
| `cancel-model-before-dispatch` | Model đang chờ → cancel | Cancelled, chưa dispatch, state_version không tăng |
| `large-route-checkpoint` | Tạo tuyến 10.000 điểm → snapshot/checkpoint | Snapshot phục hồi đủ geometry; checkpoint hội thoại không chứa geometry |
| `round-trip-charging` | Yêu cầu đi và quay lại → chọn điểm | Có hai waypoint routes và kế hoạch sẵn sàng; fixture này không đòi dừng sạc |
| `sam-son-deictic-followup` | Draft Sầm Sơn → chọn → “đến đấy chơi… quay về… cần sạc không” | Phải cập nhật round_trip/hai chặng; đây là regression tự nhiên, không ép semantic |
| `sam-son-followup-semantic-contract` | Cấp semantic trip.plan round_trip → xác nhận điểm mới | Graph dựng được kế hoạch hai chặng khi semantic đúng; chưa tự chạy |

Barrier và semantic injection là công cụ kiểm contract, không mô phỏng đầy đủ HTTP
mất gói, reconnect browser hay chất lượng model. Ca `lost_response` chủ động bỏ qua
response rồi đọc receipt trong runtime thật; mất kết nối trình duyệt cần E2E riêng.
Ca hai tab là hai `client_id`/revision trong cùng runtime, chưa phải hai browser thật.
Benchmark P/D trong `navigation_controls_benchmark.py` có tối thiểu 100 thao tác cho
mỗi tải bình thường/model chờ/map chờ; xem report để biết số mẫu riêng P và D.

Các ca chưa bao phủ trong runner chính: một vòng sạc có station/charging progress,
progress đồng thời với replace route, vehicle adapter lỗi sau dispatch với effect
chưa rõ, shutdown ở chính thời điểm checkpoint đang ghi, socket reconnect thật,
SDK/style/tile retry, multiworker và speech. Tests chuyên biệt/UI artifacts vẫn giữ;
report chỉ nghiệm thu phần đã chạy trên revision hiện tại.
