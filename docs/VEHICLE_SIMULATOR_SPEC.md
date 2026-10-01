# Vehicle Simulator Service — đặc tả MVP

**Trạng thái:** MQTT MVP và xác nhận hội thoại cho cửa/kính đã triển khai

**Ngày:** 2026-09-23

**Phạm vi đã chốt:** service MQTT độc lập; HVAC, media, cửa sổ, cửa xe, ghế và trạng thái xe; mô phỏng success, delay, timeout, reject, disconnected và state mismatch.

**Tiến độ 2026-09-23:** đã có lõi simulator/HTTP local độc lập trong `vehicle_simulator/`, MQTT transport, cấu hình broker local có ACL và MQTT adapter tích hợp backend ViVi. Backend yêu cầu xác nhận hội thoại một lần cho lệnh cửa kính/cửa xe; ViVi cũng hiểu intent cửa và sưởi ghế.

## 1. Mục tiêu

Tạo một service mô phỏng xe chạy độc lập với FastAPI ViVi. Service nhận lệnh cabin qua MQTT, cập nhật trạng thái của một xe mô phỏng, phát acknowledgement và state mới để backend có thể xác minh kết quả. Không có kết nối CAN hoặc xe thật.

Một lượt điều khiển chỉ được xem là **verified** khi backend nhận đúng acknowledgement của lệnh và đọc được trạng thái thực tế khớp giá trị yêu cầu. Việc publish MQTT thành công hoặc chỉ nhận acknowledgement `applied` chưa đủ để báo thành công.

## 2. Hiện trạng sau triển khai

- `src/vivi/vehicle/memory.py` là adapter in-memory dành cho development/test;
  `src/vivi/vehicle/mqtt.py`
  kết nối simulator độc lập qua broker, acknowledgement và state snapshot.
- `src/vivi/orchestration.py` là facade runtime duy nhất. Action luôn đi qua
  `VehicleActionGateway`, safety policy, confirmation và bước verify; không còn
  orchestrator riêng cho vehicle memory.
- Khi MQTT được bật, frontend không cung cấp `vehicle_state`; backend lấy state từ simulator trước khi quyết định action. Trường này vẫn tồn tại để tương thích với chế độ memory.
- Khi backend offline, frontend không tự đổi state cabin hoặc báo thao tác đã thành công.
- PRD yêu cầu command có correlation ID, expiry và idempotency key; kết quả phải được xác minh bằng acknowledgement phù hợp và state đọc lại.

## 3. Trách nhiệm và ranh giới

| Thành phần | Trách nhiệm |
|---|---|
| ViVi backend / safety gateway | Diễn giải intent, kiểm tra role, giới hạn, trạng thái mới nhất và xác nhận một lần cho cửa/kính; chỉ sau đó mới publish command. Theo dõi timeout và quyết định câu trả lời cho người dùng. |
| MQTT broker | Chuyển command, acknowledgement, state và availability; ACL chỉ cho backend publish command và chỉ cho simulator publish acknowledgement/state. |
| Vehicle Simulator service | Sở hữu state theo `vehicle_id`, validate schema và bất biến trạng thái, xử lý lệnh một lần, phát acknowledgement/state, lưu state và lịch sử idempotency cục bộ, tạo lỗi theo kịch bản test. |
| Frontend | Gửi ý định và hiển thị kết quả backend; không publish MQTT hoặc cung cấp state để ghi đè simulator. |

Simulator **không** quyết định approval của người dùng. Tuy nhiên, nó vẫn từ chối command sai kiểu, hết hạn, không hỗ trợ hoặc vi phạm bất biến nội tại. Các thao tác R3 như phanh, lái, truyền động và tắt hệ thống an toàn không có command handler.

## 4. State và command catalog

Mỗi `vehicle_id` có một state hiện hành và `state_version` tăng đơn điệu khi giá trị state thay đổi. Lệnh đặt lại cùng giá trị không cần tăng version nhưng vẫn phải trả ack và snapshot đúng. State tối thiểu:

| Nhóm | Field | Giá trị MVP |
|---|---|---|
| Identity | `vehicle_id`, `state_version`, `updated_at` | ID ổn định, version nguyên không âm, UTC timestamp |
| Vehicle | `driving`, `battery_percent`, `range_km` | boolean, 0–100, số km không âm |
| HVAC | `temperature_celsius` | 16–30°C |
| Window | `window_driver_percent` | 0–100% |
| Door | `door_driver_locked`, `door_driver_open` | boolean; không mở được khi đã khóa hoặc đang lái; không khóa khi cửa đang mở |
| Media | `media_playing` | boolean |
| Seat | `seat_driver_heat_level` | 0–3; giả định MVP cho “seat comfort” |

Command được hỗ trợ:

| Action | Tham số | Kết quả |
|---|---|---|
| `climate.set_temperature` | `value_celsius` | Đặt nhiệt độ tuyệt đối |
| `window.set_position` | `position_percent` | Đặt vị trí kính bên tài |
| `media.play`, `media.pause` | Không có | Đổi trạng thái media |
| `door.set_lock` | `locked` | Khóa/mở khóa cửa bên tài |
| `door.set_open` | `open` | Mở/đóng cửa bên tài |
| `seat.set_heat_level` | `level` | Đặt sưởi ghế bên tài |
| `vehicle.get_state` | Không có | Trả snapshot hiện hành; không tăng `state_version` |

Giới hạn cabin và điều kiện an toàn cần được kiểm tra ở safety gateway ngay trước publish. Simulator kiểm tra lại các giới hạn cơ bản; ví dụ từ chối mở cửa hoặc tăng độ mở cửa sổ khi `driving=true`. Chuyển đổi `driving`, pin và quãng đường là thao tác fixture của kỹ sư/test, không phải command cho tài xế. Lệnh `vehicle.get_state` là read only; `expected_state_version` chỉ bắt buộc với lệnh thay đổi state.

## 5. Contract MQTT

Topic đề xuất, version `v1`. Tài liệu kiến trúc cũ dùng `vivi/{vehicle_id}/state`; contract này thêm version và sẽ thay thế topic cũ khi tích hợp:

```text
vivi/v1/vehicles/{vehicle_id}/commands
vivi/v1/vehicles/{vehicle_id}/acks
vivi/v1/vehicles/{vehicle_id}/state
vivi/v1/vehicles/{vehicle_id}/availability
```

Command là JSON có schema version. Các trường trong ví dụ là bắt buộc với lệnh thay đổi state:

```json
{
  "schema_version": 1,
  "command_id": "uuid",
  "correlation_id": "turn-id-or-trace-id",
  "idempotency_key": "vehicle-id:turn-id:step-id",
  "vehicle_id": "demo-car-1",
  "action": "climate.set_temperature",
  "arguments": {"value_celsius": 24},
  "issued_at": "2026-09-23T10:00:00Z",
  "expires_at": "2026-09-23T10:00:05Z",
  "expected_state_version": 7
}
```

Acknowledgement luôn mang `command_id`, `correlation_id`, `idempotency_key`, `vehicle_id`, `state_version`, `status`, `reason_code` nếu có và thời điểm xử lý. `status` chỉ là `applied`, `rejected` hoặc `duplicate`. `applied` có nghĩa simulator đã xử lý command và ghi state nếu có thay đổi; backend vẫn phải đối chiếu snapshot state. `duplicate` trả lại kết quả gốc mà không thực hiện thêm; ack này phải cho biết kết quả gốc là `applied` hay `rejected`. Một idempotency key được dùng lại với action/arguments khác phải bị từ chối `idempotency_conflict`.

Reason code tối thiểu: `invalid_schema`, `unknown_action`, `invalid_arguments`, `expired`, `vehicle_mismatch`, `state_version_conflict`, `policy_invariant`, `idempotency_conflict` và `simulated_reject`. Payload lỗi vẫn phải giữ các ID có thể đọc được để backend ghép đúng lượt; payload không parse được phải được ghi log và bỏ qua an toàn.

State message chứa snapshot đầy đủ cùng `vehicle_id`, `state_version`, `updated_at`. Simulator publish state sau mỗi thay đổi và khi khởi động lại; backend cũng cần cách chủ động yêu cầu `vehicle.get_state` để đọc lại state mới nhất. Availability báo `online`/`offline` để backend fail closed khi service ngắt kết nối.

Quy tắc truyền tin:

- Không retain command. State và availability có thể retain để người mới subscribe nhận snapshot/health gần nhất; backend phải kiểm tra timestamp/version trước khi dùng cho quyết định safety.
- Command phải có expiry. Lệnh đến muộn, version cũ, schema sai hoặc `vehicle_id` không khớp bị từ chối và không thay đổi state. Kiểm tra idempotency trước khi từ chối một bản giao lại đã hoàn tất vì expiry/version đã đổi.
- Cùng command được giao lại phải cho cùng kết quả. Khóa idempotency phải gắn với `vehicle_id`; không dùng `turn_id` toàn cục như hiện tại.
- Khi chưa biết kết quả do timeout hoặc mất ack, backend đọc state để chẩn đoán và trả `unverified` nếu không có ack gốc phù hợp. Backend chỉ được thử lại bằng cùng `command_id`/`idempotency_key` khi command chưa hết hạn; không tự tạo command mới cho cùng ý định.
- Ack `duplicate` chỉ có thể góp phần xác minh nếu nó xác nhận kết quả gốc là `applied`, giữ đúng identity của command gốc và state đọc lại khớp yêu cầu.
- MQTT có thể giao lại hoặc giao chậm; service phải xử lý command theo thứ tự có kiểm soát cho mỗi xe. `expected_state_version` ngăn lệnh được quyết định trên snapshot cũ ghi đè state mới.

## 6. Fault injection cho kiểm thử

Chỉ bật qua cấu hình/test control dành cho kỹ sư. Không nhận fault mode trong command của tài xế.

| Mode | Hành vi cần mô phỏng | Điều cần quan sát |
|---|---|---|
| `success` | Áp lệnh, phát state và ack đúng | Backend trả `verified` |
| `delay` | Trì hoãn xử lý hoặc ack theo thời gian cấu hình | Dưới ngưỡng thì thành công; quá ngưỡng thì timeout |
| `timeout_before_apply` | Không áp lệnh và không trả ack | State giữ nguyên; backend không báo thành công |
| `ack_lost_after_apply` | Áp lệnh nhưng bỏ ack | Backend phải đọc state và xử lý trạng thái chưa chắc chắn; không gửi lệnh mới bừa bãi |
| `reject` | Trả ack `rejected` và reason code | State giữ nguyên; backend báo từ chối/lỗi |
| `disconnected` | Simulator ngắt broker hoặc không nhận command | Backend phát hiện unavailable/timeout |
| `state_mismatch` | Ack `applied` nhưng state không đạt giá trị yêu cầu | Backend không trả `verified` |

Fault được chọn theo command hoặc test scenario với seed/cấu hình cố định để chạy lại cho cùng kết quả. Control topic/API của fault injection phải tách khỏi command topic và chỉ dùng trong môi trường test/demo.

## 7. Lưu trữ và vòng đời

- State và bảng idempotency được ghi cùng một transaction cục bộ trước khi phát ack/state, để restart không làm thực thi lại command đã hoàn tất. SQLite là lựa chọn mặc định phù hợp với một service local; đường dẫn cấu hình qua env.
- Có fixture reset về state mặc định cho test. Reset tạo `state_version`/epoch mới hoặc xóa session test được chỉ định, không âm thầm ghi đè xe đang chạy demo.
- Service hỗ trợ ít nhất một `vehicle_id` mặc định. Mọi command/state/ack đều có `vehicle_id` để sau này chạy nhiều xe mà không đổi contract.
- Ghi log có `command_id`, `correlation_id`, action, outcome, latency, state version và fault mode; không cần lưu transcript/audio trong service này.

## 8. Hạng mục triển khai

1. Định nghĩa Pydantic schema cho state, command, acknowledgement, error code và fixture; khóa contract bằng test.
2. Tạo MQTT broker local và cấu hình ACL/topic; thêm service simulator vào Docker Compose hoặc lệnh chạy local tương đương.
3. Xây dựng service simulator: state store, command dispatcher, idempotency, expiry, state version, MQTT publish/subscribe và availability.
4. Bổ sung command catalog HVAC, media, window, door, seat và `get_state`.
5. Tạo fault injection có thể kiểm soát và test từng mode.
6. Thay lời gọi trực tiếp `VehicleSimulator` trong backend bằng MQTT vehicle adapter; safety gateway đọc live state trước khi quyết định và xác minh ack + state sau lệnh.
7. Cập nhật `/api/v1/turn`, frontend state và hướng dẫn chạy để bỏ việc dùng state do trình duyệt cung cấp làm nguồn sự thật.
8. Viết unit, contract và integration test với broker thật; chạy toàn bộ luồng text → safety → MQTT → simulator → verify.

Thứ tự triển khai ban đầu: **contract + broker → simulator độc lập → test lỗi và khôi phục → backend adapter + safety/HITL → frontend và end to end**. Backend hiện đã có `unverified` và `confirm`; nút đổi chế độ lái trên frontend được vô hiệu hóa khi dùng MQTT. Fixture lái xe chỉ thay đổi qua test control local.

## 9. Tiêu chí nghiệm thu

- [x] Service khởi động độc lập; backend và simulator trao đổi được qua broker local khi không có WAN.
- [x] Mỗi action trong catalog cập nhật đúng field, phát ack có cùng `command_id`/`correlation_id` và phát state với version tăng đúng.
- [x] `vehicle.get_state` trả snapshot hiện hành; frontend reload không ghi đè state bằng mặc định của trình duyệt.
- [x] Nhánh frontend chạy khi backend offline không báo thao tác cabin là đã được simulator xác minh.
- [x] Hai lần giao cùng idempotency key và payload chỉ áp lệnh một lần; cùng key khác payload bị từ chối; kết quả này vẫn đúng sau restart.
- [x] Trạng thái và kết quả idempotency vẫn nhất quán khi service dừng ngay sau khi ghi state nhưng trước khi phát ack.
- [x] Command hết hạn, sai schema, sai vehicle, sai state version hoặc vi phạm giới hạn không đổi state.
- [x] Mở cửa và tăng độ mở kính khi đang lái bị chặn; mọi command R3 không có handler.
- [x] Cửa/kính chỉ được backend publish sau approval hợp lệ gắn với đúng action; deny, timeout, replay approval không phát command.
- [x] Backend chỉ trả `verified` khi ack `applied` khớp ID và state đọc lại khớp giá trị yêu cầu.
- [x] Với delay, timeout trước/sau apply, reject, disconnected và state mismatch, backend không báo sai thành công; mỗi case có test tái hiện được.
- [x] Service không có đường command trực tiếp từ frontend; ACL broker giới hạn publisher theo trách nhiệm đã nêu.
- [x] Log của một lệnh cho phép nối trace từ backend đến simulator bằng `correlation_id`.

## 10. Ngoài phạm vi MVP

- CAN adapter hoặc xe thật.
- Điều khiển phanh, lái, truyền động, pin và quãng đường bằng intent của tài xế.
- Nhiều vùng điều hòa, nhiều cửa/kính/ghế và các tính năng ghế ngoài sưởi ghế bên tài.
- RAG cẩm nang, STT/TTS, route planning, OTA và dashboard kỹ sư đầy đủ.
- MQTT qua mạng công cộng hoặc hạ tầng production nhiều node.

## 11. Giả định cần xác nhận trước khi triển khai

1. “Ghế” trong MVP được hiểu là **sưởi ghế bên tài mức 0–3**. Nếu nhóm muốn chỉnh vị trí, ngả lưng hoặc massage, cần đổi command catalog trước khi code.
2. “Cửa” gồm **khóa/mở khóa** và **mở/đóng cửa bên tài**. Xác nhận người dùng áp dụng cho mọi thay đổi cửa/kính theo PRD; policy chi tiết khi xe đang lái cần được khóa trong safety spec.
3. Một service local và một broker local là profile chạy đầu tiên. Contract vẫn gắn `vehicle_id` để mở rộng sau.
