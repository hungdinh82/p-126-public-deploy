# Runtime contracts

Đây là contract khóa giữa bốn workstream. Thay đổi field hoặc semantics cần review của
platform, agent và vehicle owner.

## Nguồn sự thật

- UI sở hữu audio/touch state, không sở hữu vehicle state.
- Vehicle adapter/MQTT state là nguồn sự thật cho policy và kết quả action.
- LLM chỉ tạo `ActionProposal`; không tạo MQTT payload.
- Tool result đã verify là nguồn sự thật cho câu “đã thực hiện”.
- Handbook claim chỉ hợp lệ khi ánh xạ tới `source_id` trong retrieval result.

## Turn

`POST /api/v1/turn`:

```json
{
  "transcript": "Đặt nhiệt độ 25 độ",
  "session_id": "trip-01",
  "turn_id": "turn-01",
  "llm_provider": "rules"
}
```

Response luôn có `session_id`, `turn_id`, `trace_id`, `route`, `status`, `message`,
`vehicle_state`, `latency_ms` và `trace`. `status` thuộc tập:

```text
verified | clarify | blocked | confirmation_required | denied | expired |
unsupported | unverified | error
```

`vehicle_state` gửi từ client hiện còn trong schema để chuyển đổi phiên bản nhưng bị bỏ qua
tại policy boundary.

## Action catalog

| Intent | Arguments | Risk |
|---|---|---|
| `vehicle.get_status` | `{}` | R0 |
| `climate.set_temperature` | `value_celsius: 16..30` | R1 |
| `media.play`, `media.pause` | optional `media_query` | R1 |
| `seat.set_heat_level` | `level: 0..3` | R1 |
| `window.set_position` | `position_percent: 0..100` | R2 |
| `door.set_open` | `open: bool` | R2 |
| `door.set_lock` | `locked: bool` | R2 |

R2 luôn tạo confirmation gắn `action hash + session + vehicle_id + state_version + expiry`.

## Confirmation

`POST /api/v1/confirmations/{confirmation_id}`:

```json
{
  "session_id": "trip-01",
  "turn_id": "turn-02",
  "decision": "approve",
  "llm_provider": "rules"
}
```

Approval là single-use. Sai session, replay, expiry, action thay đổi hoặc `state_version`
thay đổi đều không execute.

## Local demo fixture

`PUT /api/v1/demo/vehicle/driving` chỉ đổi cờ `driving` của memory simulator cho phiên UI:

```json
{"session_id": "trip-01", "driving": true}
```

Endpoint trả `409` khi runtime dùng MQTT. Đây không phải vehicle command và không được dùng
để thay state xe thật; safety gateway vẫn đọc lại state từ adapter trước mỗi action.

## Streaming

`POST /api/v1/turn/stream` trả NDJSON:

```json
{"type":"speech","text":"..."}
{"type":"final","streamed_speech":true,"response":{}}
```

Không phát speech xác nhận thành công trước safety/ACK/read-back. WebSocket chỉ được thêm
sau khi HTTP/NDJSON đạt benchmark Nano; không tạo graph hoặc response contract thứ hai.

## Versioning

- Breaking API change tạo `/api/v2`, không âm thầm đổi meaning của field.
- MQTT command bắt buộc có command ID, correlation ID, idempotency key, expiry và expected
  state version.
- Handbook artifact ghi checksum nguồn và scope model/year/locale.
