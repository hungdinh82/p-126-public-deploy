# Local tools: giao diện nhỏ cho model, contract đầy đủ ở host

`local_semantic_contract="operations_v1"` nay là **mặc định** (config.toml và
`config.py`); version trong log là `local-operations-3`. Đặt `legacy` để quay lại
bộ interpreter một-schema khi cần so sánh/gỡ lỗi. Không cần đổi weights hoặc
inference server Jetson.

Review mới nhất: [tool/schema vòng 2 + đổi mặc định](investigations/local-tool-schema-v3-2026-10-17.md)
(nối tiếp [binding và đối chứng model thật](investigations/local-tool-review-2026-10-17.md)).

## Model thực sự nhận gì?

Model chọn thao tác và điền tham số nghiệp vụ. Không nhận DTO SemanticTurn,
form identity/revision, boolean actuator, exclusion mode hoặc reference flags.
Ví dụ output gốc:

```json
{"calls":[{"name":"play_music","arguments":{}}]}
```

| Tool | Tham số model cung cấp | Host chuyển đổi |
|---|---|---|
| play_music | không có | media.play({}) |
| play_requested_music | query | media.play(media_query) |
| pause_music | không có | media.pause({}) |
| set_temperature | celsius: 16–30 hoặc null | value_celsius; null giữ thiếu để hỏi |
| increase_temperature / decrease_temperature | degrees dương hoặc null | delta có dấu; null dùng 2°C |
| open_door / close_door | targets | open=true/false |
| lock_door / unlock_door | targets | locked=true/false |
| raise_window / lower_window | targets | độ mở 0/100 |
| set_window | targets, percent | position_percent |
| set_seat_heat | targets, level: 0–3 hoặc null | level; null giữ thiếu |
| get_directions / calculate_trip | destination | purpose=navigate/itinerary |
| các biến thể _round_trip / _from | destination, origin khi cần | trip_kind/origin_query |
| update_current_request | các giá trị sửa được của form | ID/revision/bước từ snapshot |

`targets` là một mảng duy nhất: driver/front_passenger/rear_left/rear_right,
all, left/right, front/rear hoặc same. `[]` là chưa biết vị trí; `["left"]` chỉ
xác định bên, không tự chọn hàng ghế. Mảng concrete biểu diễn nhiều vị trí;
side+row được host chuyển thành tọa độ tương ứng. Các tập mâu thuẫn bị từ chối.
Tham số loại trừ chỉ xuất hiện trên biến thể `_except` khi cần tài liệu đó.

Chữ ký tool chỉ chứa trường liên quan và đóng bằng additionalProperties=false.
Trường bắt buộc chỉ áp dụng cho các tham số nghiệp vụ của thao tác đó; không chạy
strict_schema để bắt model trả mọi slot chung. Mô tả và giới hạn đầy đủ vẫn ở
[canonical catalog](local_tool_catalog.json). `fresh_session_tools` trong catalog
là schema model-facing; `canonical_documents` và `bindings` thuộc host, không gửi
lên model. Native transport cũng bỏ `_binding` trước khi gửi.

## Vì sao có nhiều field null trong report cũ?

`results[].semantic` là đối tượng domain SAU adapter/Pydantic. Các field nullable,
flags và mảng mặc định được thêm khi model_dump(); chúng không phải raw output.
Đọc `attempts[].proposal_attempts[].calls` để xem model thực sự sinh gì.
Runner hiện xuất semantic với exclude_none/exclude_defaults và ghi rõ
`semantic_representation`. Assertions vẫn kiểm trên domain object đầy đủ.

## Context, prompt và validation

- Discovery tối đa tám capability; không khớp từ không có nghĩa không hỗ trợ.
  Tối đa hai inference, lần thứ hai cho discovery hoặc repair.
- System prompt giữ quy tắc chung; chữ ký/tool guide ngắn thay cho chèn cả JSON
  schema. Unit/bounds và ranh giới ý định nằm ở từng tool. JSON grammar vẫn nhận
  schema đầy đủ để ràng buộc cấu trúc.
- Tối đa bốn ví dụ, chọn theo nhóm tool đang được đưa ra: cả hai chiều mở/đóng, ca
  thiếu vị trí và ca có vị trí; khi có hai nhóm thì thêm một câu ghép. Ví dụ chỉ được
  giữ nếu hợp lệ với schema của lượt đó. Không nhóm nào chỉ có ví dụ phủ định (ví dụ
  cũ "đừng mở cốp" khiến "đóng cốp giúp tôi" thành reply/clarify).
- Prompt đề xuất không còn topic policy: model này chỉ chọn route, không viết câu trả
  lời; policy vẫn áp dụng ở bước responder.
- Context bỏ lời trợ lý cũ không có typed draft, chỉ giữ form/question/tham chiếu
  cần dùng. Model không chép ID, revision, câu nói hoặc location_text.
- Lệnh chỉ điều khiển xe không nhận địa điểm/chủ đề navigation cũ. Câu hỏi thông
  tin và navigation vẫn có tham chiếu cần dùng; form/task giữ riêng.
- Telemetry không vào prompt. "Các cửa còn lại/đang mở" là `targets=["remaining"]`
  (chỉ có khi câu nói có ý lọc); host tính tập cửa từ `vehicle_observation`, đối
  chiếu số cửa được nói, và hỏi lại khi thiếu trạng thái hoặc số không khớp.
- `dialogue_reply` chỉ được đưa ra khi câu có chào/cảm ơn; `set_window` chỉ khi có
  số/phần trăm; `web_search` + `knowledge_lookup` gộp thành `lookup_information`,
  `information.policy` chọn nguồn.
- Create của cùng operation vẫn có mặt khi đang chờ form cũ; update chỉ sửa
  form. Tác vụ navigation dùng lookup theo capability/purpose thay tên alias.
- Host lưu input_utterance và raw proposals. Không bắt model chép lại câu nói.
- Host veto (`clause_guard`), chỉ chặn chứ không bao giờ tạo action/slot:
  - phủ định/điều kiện/hẹn giờ (đừng, chớ, không cần, nếu, khi nào, lát nữa, sau N
    phút…) trong câu có capability tác động: không gọi model, trả clarify; nếu có
    tác vụ/câu hỏi đang sống thì chỉ cho model hủy/trả lời/clarify. So khớp có dấu.
  - độ phủ vế: tách vế bắt đầu bằng động từ điều khiển; vế không khớp capability
    nào mà bị bỏ hoặc thay bằng thao tác khác thì chặn cả batch (unsupported, không
    repair); vế có capability nhưng bị bỏ thì repair một lần rồi hỏi lại.
  - vị trí cụ thể phải có từ tương ứng trong câu (rear_* cần "sau", …), nếu không
    thì repair về left/right/front/rear/[].
- Câu không dấu từ STT không được veto từ vựng bảo vệ; vẫn phụ thuộc model.
- Một dialogue_clarify/unsupported hợp lệ là veto cho toàn bộ batch, kể cả khi
  model trả kèm action. Không repair tổ hợp này thành thực thi một phần.
- Backend giữ kiểm tra type/bounds, số literal, tọa độ nhất quán, form identity,
  consent và policy. Không viết lại action sai thành đúng bằng parser.
- OK thuần/lựa chọn số dùng grammar ngắn hiện có và AnswerResolver kiểm tra
  binding/hạn dùng; không báo calls=0 thành model hiểu đúng.
- diagnostics.prompt_version lấy contract của attempt served; log cũ luôn ghi
  semantic-core-4 nên phải đọc semantic_contract trong provider_attempts.
- Model không tự cấp consent hoặc khởi hành. Cloud, typed UI, reducer, ledger,
  suspend/cancel và scheduler giữ contract hiện tại.

## Thử và kiểm chứng

```toml
local_semantic_contract = "operations_v1" # legacy để quay lại
local_semantic_transport = "json_schema" # native để so sánh
```

Có thể dùng LOCAL_SEMANTIC_CONTRACT/LOCAL_SEMANTIC_TRANSPORT rồi restart backend.
Kết quả local-operations-3: [review tool/schema 17/10 (vòng 2)](investigations/local-tool-schema-v3-2026-10-17.md).

```bash
PYTHONPATH=. .venv/bin/python scripts/export_local_tools.py
.venv/bin/python -m eval.semantic_benchmark --provider local \
  --contract operations_v1 --transport json_schema \
  --dataset eval/local_tool_regression_cases.jsonl --output /tmp/local-tools.json
VIVI_TEST_JETSON_URL=http://100.123.27.125:8789 \
VIVI_TEST_FORM_REPORT=/tmp/local-forms.json \
.venv/bin/pytest --run-live -q -s tests/integration/test_local_forms_live.py
```

Live workflow dùng xe bộ nhớ và SQLite tạm, không dùng phiên xe người dùng.
Budget là production; STT/TTS không nằm trong phép đo semantic.
`inference_breakdown` tách số response, prompt/output tokens, cache, prefill/decode;
không đếm trùng completion cuối trong gateway và trong proposal attempts.

Hai corpus hiện có đã dùng phát triển, không phải holdout. Fallback không tính
đúng. Pass cuối tách khỏi passed_first_call. Chỉ bật mặc định sau nghiệm thu riêng:
>=95% đúng toàn bộ proposal theo nhóm; không có effect trái yêu cầu trong ca
phủ định/điều kiện/thiếu nghĩa/stale consent; p95 không tăng quá 20% so baseline
cùng corpus và điều kiện cache.
