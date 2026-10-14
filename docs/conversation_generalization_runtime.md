# Hợp đồng hội thoại và địa điểm — 14/10/2026

Runtime vẫn dùng một LangGraph, reducer/scheduler và NavigationService hiện có. Các phần mới nằm ở ranh giới diễn giải câu trả lời và tìm thực thể, trước khi tính tuyến, năng lượng và cấp quyền chạy xe.

## Câu hỏi thuộc checkpoint

`QuestionProposal` khai báo purpose, owner, target/revision, slot và hiệu ứng của từng lựa chọn. Reducer lưu `PendingQuestion`, gắn task/step, ID và thời hạn. Bốn purpose: `collect_slot`, `select_entity`, `confirm_proposal`, `approve_execution`.

`dialogue/questions.py` là adapter duy nhất đọc phase và offer cũ. Capability node xuất proposal qua adapter này sau khi cập nhật domain; reducer bind vào checkpoint. Đây là bước chuyển đổi tương thích, chưa xoá các field phase/query trong state cũ. Proposal tường minh có thể thay semantics của adapter nếu cùng owner/target/revision; renderer không suy nghĩa yes/no từ wording.

ID bao gồm liên kết state và semantics/effects. Đổi wording không đổi ID; đổi đích, revision, lựa chọn hoặc hiệu ứng làm ID cũ không còn hợp lệ. Thời hạn theo task/grant, hoặc ba phút cho câu hỏi domain không còn step chờ. Reply cũ bị từ chối trước khi câu hỏi hết hạn được phát lại. Confirmation quyền thực thi tiếp tục dùng grant/expiry và policy hiện có.

`AnswerResolver` dùng chung cho voice và UI: yes/no, số thứ tự, tên ứng viên, chọn handle, bổ sung tên/khu vực và scalar slot. Lời đáp rõ đi qua rules, không cần gọi model. Adapter `change_destination` giữ polarity của checkpoint cũ; “OK” giữ đề xuất đã có. Chọn POI không cấp quyền khởi hành.

Response có `pending_question` với ID, owner, target/revision, purpose, slot, choices và `accepts_yes/no`; không gửi executable effects ra client. UI mới gửi:

```json
{
  "session_id": "driver",
  "turn_id": "choose-2",
  "question_reply": {"question_id": "question:…", "option_id": "place:…"}
}
```

`option_id` là handle của choice, hoặc `yes`/`no` khi câu hỏi chấp nhận. Để bổ sung slot, gửi `value` dạng chuỗi thay cho option. Chỉ được gửi một trong hai. Kiểu và giới hạn được kiểm tra lại bởi contracts/capability. Endpoint chọn rules cho typed reply; không phụ thuộc model khả dụng. Action và confirmation endpoint cũ vẫn hoạt động qua validation hiện có.

## DestinationSpec và nguồn khu vực

Navigation/trip nhận `arguments.destination` version 1; `query` cũ được chuyển sang cùng cấu trúc. Dữ liệu gồm `raw_text`, `name`, `entity_type`, `area`, `selection`, `qualifiers`, `search_hypotheses`, `reference`, `nearest`.

Selection phân biệt `named_place`, `category`, `region`, `reference`. Reply khu vực chỉ đổi `area`, giữ tên và `question_id` của câu hỏi; bổ sung tên vào task đang chờ giữ những slot khác và các bước tiếp theo. Reference phải là handle thuộc tập ứng viên hoặc điểm đã chọn trong session hiện tại; không tìm lại bằng tên và không tạo tọa độ.

Qualifiers như “cổng 1” được giữ trong query cả khi có khu vực ưu tiên. Model chỉ được dùng chi tiết người dùng đã nói hoặc chi tiết của câu hỏi area hiện hành; không tự thêm cổng/cơ sở. Adapter chuyển selection/category/nearest sang workflow cũ để request typed và query legacy có cùng hành vi.

Area có `source` user/answer/context/position/hypothesis và `constraint` required/preferred. Khu vực người dùng nói là ràng buộc. Khu vực từ vị trí hoặc giả thuyết model luôn là ưu tiên, không loại đích xa. SemanticResolver kiểm tra raw text, nguồn khu vực và reference trước guard. Tên model suy thêm được đưa vào search hypothesis chưa xác minh, không thành alias chính thức.

## Phân giải trước khi tính tuyến

Navigation và trip dùng cùng một `PlaceResolver` trong NavigationService. Resolver không có hiệu ứng lên xe. Đầu ra phân biệt `resolved`, `ambiguous`, `missing_constraints`, `no_verified_match`, `provider_unavailable`; giữ reason, required slot, queries, evidence và lỗi đọc.

- Query planner dùng tối đa hai biến thể tên: tên gốc/alias đã biết, hoặc giả thuyết có đánh dấu. Search layer hành chính có fallback CITY→WARD trong cùng budget.
- Search/detail/đọc travel thuộc một budget mặc định bốn giây mỗi resolution; hết hạn hủy và drain tác vụ con. Các lần đọc độc lập đồng thời tối đa ba tác vụ.
- Cache tối đa 128 mục, TTL 30 giây, key theo query và tham số near/layer/category. Cache chỉ metadata provider ở dạng JSON bất biến. Mỗi lần đọc tạo handle mới và kiểm tra khu vực lại; không cache grant, road, SOC hay kết quả sạc ở đây.
- Bằng chứng gồm mức khớp tên, loại từ metadata/tên, địa chỉ/tọa độ và dấu hiệu branch/subunit/street. Giữ các cổng/chi nhánh tách biệt; vị trí gần nhau không đủ chứng minh cùng địa điểm. Metadata không chứng minh khu vực bắt buộc thì không chọn.
- Alias catalogue hỗ trợ spelling, không quyết định tên nào được phép dẫn đường. Tên ngoài catalogue đi cùng pipeline. Hypothesis luôn cần người dùng chọn, kể cả model đề nghị chọn tự động.
- Plan/refine thông thường chưa lấy tuyến tới mọi kết quả autocomplete. Search liệt kê và yêu cầu gần nhất vẫn có travel/matrix khi cần; tuyến và sạc tiếp tục được kiểm tra khi chuẩn bị preview/khởi hành.

Rules chọn được điểm công cộng có tên/địa chỉ/tọa độ đủ rõ và không có ứng viên cạnh tranh hợp lý. Điểm trùng tên, thiếu metadata, cơ sở/cổng vẫn hỏi lại. Model phân biệt POI chỉ được chọn handle có thật; không cấp quyền chạy xe. Các điểm số và margin hiện là chính sách khởi đầu, không phải confidence đã được hiệu chỉnh. Metadata hierarchy đầy đủ vẫn phụ thuộc adapter bản đồ.

Không có kết quả và chưa có khu vực: hỏi area. Đã có khu vực bắt buộc mà không có match: xin tên/địa chỉ khác. Provider lỗi: thông báo lỗi bản đồ thay vì hỏi lại tỉnh/thành như thể người dùng nói sai.

## Budget và quan sát

`SEMANTIC_BUDGET_SECONDS` mặc định 2.5 giây là tổng thời gian chờ provider hiểu ý định trong lượt, chia cho cloud/local; dành một phần cho local fallback. Không phải SLA của toàn lượt: STT, tìm POI, phán định ứng viên, lấy tuyến, sạc và TTS còn có budget riêng. Rules/UI reply không dùng phần model này.

Metrics thêm `navigation_place_resolution`; search/detail/matrix/route/energy và tổng wall time tiếp tục được ghi riêng. Các stage lồng nhau/đồng thời không cộng thành tổng latency. Selection diagnostics giữ resolution, queries và evidence khi cần hỏi/chọn.

## Kiểm chứng và giới hạn

`tests/test_conversation_generalization.py` kiểm tra UI/voice, stale/expiry/owner/effect, provenance khu vực trong domain state, scalar slots, bảo toàn chuỗi/khứ hồi, tên và khu vực tổng hợp chưa nằm trong catalogue, cache isolation, timeout/drain, metadata lỗi, hypothesis, semantic fallback budget và reference không search lại.

Các regression cũ vẫn kiểm tra consent, concurrency, SOC, sạc, hành trình, restart và API/MQTT. Các fixture mới dùng dữ liệu và tọa độ giả; không dùng tài khoản map hay gọi provider thật. Chưa đo entity accuracy hoặc p50/p95 trên STT/provider thực tế. Thay đổi này tạo contract chung và giảm việc sửa từng địa danh; vẫn cần bổ sung dữ liệu provider và tập đánh giá độc lập cho những trường hợp thiếu bằng chứng.

Chạy gate bằng `bash scripts/check_local.sh`. Khởi động lại backend để nạp code/config; schema mới có default/adapter đọc checkpoint cũ, không yêu cầu xoá lịch sử hay reset hành trình đang có.

Kết quả kiểm tra trong lượt triển khai:

- Quality gate: 1.008 tests passed, Python lint và frontend syntax passed.
- Phần qualifiers được bổ sung trong lúc gate đang chạy và kiểm tra riêng trên code mới nhất: toàn bộ 46 ca trong `test_conversation_generalization.py` passed. Các ca này có phần giao với gate, không cộng hai số thành tổng test.
- Kiểm tra riêng prompt/bộ nhớ: 125 passed; điều hướng/tinh chỉnh: 117 passed. Đã sửa và kiểm tra lại hồi quy stale-start khi đưa lời đáp về resolver chung.
- Whitespace, lint và cú pháp `app.js` kiểm tra lại sau thay đổi cuối đều passed.
