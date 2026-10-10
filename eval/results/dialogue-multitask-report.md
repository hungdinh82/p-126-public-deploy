# Log 910–924: điều hoà, sửa chuỗi thao tác và loại trừ

Đọc read-only 15 lượt mới trong `data/vivi_rag.sqlite3`, ngày 2026-10-09. Tất cả
đều `effective_provider=google`, không fallback. Vì vậy đây không phải lỗi do
hệ thống chỉ dùng parser offline. Trong log có cả lỗi model diễn giải, lỗi guard
ghi đè và giới hạn của reducer/context. Log gốc được giữ nguyên.

## Nguyên nhân và sửa

| Lượt | Biểu hiện | Nguyên nhân | Thay đổi |
|---|---|---|---|
| 915, 916, 921, 922 | Tăng/giảm điều hoà cứ hỏi mức nhiệt | Chính sách cũ yêu cầu lượng tăng/giảm; prompt và parser cùng áp dụng chính sách đó | Theo yêu cầu mới, mặc định ±2°C, cả “tiếp”, “một chút”, “tuỳ bạn”; số cụ thể được ưu tiên |
| 918 | “giảm điều hoà tối đa” lại đặt 30°C | Parser lấy “tối đa” thành nhiệt độ cao nhất mà không xét động từ; numeric guard dùng frame đó | Giảm tối đa → 16°C; tăng tối đa → 30°C; phân biệt nhiệt độ đích với delta |
| 923 | “tăng nhiệt độ lên 2 độ” thành đặt 2°C | Relative grammar thiếu dạng có domain + lên/xuống; guard dùng absolute frame sai | Dạng tăng/giảm một số độ nhỏ là delta; giữ target 16–30°C với “lên/xuống” như regression cũ; câu có thể mơ hồ không bị range heuristic ghi đè model |
| 923, 924 | Thêm nhạc bị hỏi “sửa bước nào” | Mọi draft bị coi là sửa các bước đã tồn tại, không có thao tác append; guard thường ghi đè cả quyết định model | Contract có `step_updates` và `append_actions`, kiểm tra toàn bộ patch rồi áp dụng, tối đa 5 bước; giữ IDs, completed effects và thứ tự |
| 913, 914 | Loại trừ thêm cửa phụ lại đưa cửa tài vào, rồi chỉ chọn cửa tài | Exclusions bị thay thế; model context thiếu danh sách loại trừ; model/parser có thể đảo nghĩa exclude → select | Mặc định cộng dồn exclusions, chỉ reset với `exclusion_mode=replace`; context cung cấp exclusions và resolved arguments; exclusion-only guard sửa đúng trường, kể cả model dùng step patch |

Quy ước điều hoà trong đợt này là **tăng/giảm nhiệt độ**, không phải tốc độ quạt.
Registry tính từ trạng thái xe đọc mới ở mỗi bước, giới hạn 16–30°C. Khi đạt
giới hạn, câu trả lời nêu mức nhiệt thực tế đã đặt. “Đặt/chỉnh nhiệt độ” chưa có
giá trị vẫn hỏi slot; default không điền bừa một target tuyệt đối.

## Model và rule-based có vai trò gì?

Cloud gọi model để tạo `SemanticTurn` JSON theo schema và capability catalogue.
Local client có thể gọi function `interpret_turn` theo cùng contract. Đây là
đề xuất có kiểu: model chọn capability, tham số, thứ tự, bước cần sửa và bước
cần thêm. Backend reducer/scheduler quyết định trạng thái, rồi gọi capability
qua validate → policy → execute → verify. Model không sở hữu command IDs hoặc
grant và không thể tự cấp quyền chạy thao tác.

Trước sửa, `bounded.act=modify` thường khiến guard trả nguyên output parser,
ghi đè quyết định của model. Một parser thiếu grammar có thể làm kết quả model
tệ đi, kể cả khi API phục vụ thành công. Sau sửa, full model plan với explicit
patch được giữ; guard chỉ ràng buộc các bằng chứng cụ thể như slot/vị trí, delta
rõ, loại trừ rõ, phủ định và phần control chưa được phân tích đủ.

Model context có current/editable step indices, stable handles, task revision,
arguments, exclusions, resolved arguments, missing slots và câu hỏi đang chờ.
Trace giữ `model_semantic` và `semantic_guard_fields`, bỏ free-form model text
khỏi đề xuất gốc để không phát tán prompt hoặc phát biểu đã làm chưa được xác
minh. Có thể đối chiếu output model với semantic thực sự đưa vào reducer.

## Multi-task và consent

- Điền slot và yêu cầu thêm nhạc giữ task/step hiện có, thêm step ID do server cấp.
- Sửa bước nhiệt độ phía sau trong lúc kính đang chờ confirmation được phép;
  scheduler vẫn dừng ở kính. Không chạy vượt bước chờ.
- Patch kiểm tra tất cả indices trước khi sửa, không sửa completed/running/
  unknown effects, không thêm quá 5 bước; revision tăng một lần.
- Chỉnh task hoặc input nói chưa hiểu sẽ thu hồi consent cũ. “Đồng ý” sau một
  clarification không được chạy kế hoạch cũ. UI event cũ chỉ bị từ chối, không
  phá grant mới đang hợp lệ.
- Parser offline hiểu cả sửa bước được chỉ định và thêm thao tác cuối chuỗi.
- Restart/replay giữ completed effects và command IDs, không gửi lại thao tác.
- Task contract 7 được nâng tương thích trong checkpoint, vô hiệu grants cũ;
  history và ledger được giữ, không reset task đã hoàn tất.

## Kiểm thử và kết quả

Fixture `audit_910_924.jsonl` giữ query và semantic output đã ghi lại, không có
session/confirmation ID hoặc GPS. Replay cả 15 lượt qua parser và hai provider
mock Google/local phát lại output lỗi cũ. Đây là kiểm tra contract/guard/reducer,
không phải accuracy NLU của model local.

Test mới bao gồm default, min/max, cumulative exclusion, model patch phía sau,
append, partial execution, từng bước đọc state mới, restart/replay, invalid patch,
duplicate indices, max steps, unclear correction và stale UI consent.
183 test liên quan đã pass; 34 test confirmation và multi-task pass sau tách
typed UI event khỏi spoken clarification. Bốn file regression frontend pass.
Full suite trên code cuối: **471 passed in 244.16s**, có chạy MQTT fixture
localhost. Ruff và `git diff --check` pass. Các phép thử dùng DB tạm và
VehicleSimulator, không phát lệnh tới xe thật.

Dataset `dialogue_multitask_cases.jsonl`: 4 episode, 17 lượt. Kết quả theo tầng
thực sự phục vụ, dùng `latency_ms.total` của orchestrator:

| Tầng | Lượt đạt | Episode đạt | Hỏi lại | p50 / p95 | Thực thi sai theo oracle |
|---|---:|---:|---:|---|---:|
| Parser | 17/17 | 4/4 | 1 | 331.84 / 486.72 ms | 0 |
| Google + guard | 17/17 | 4/4 | 1 | 2456.98 / 3676.61 ms | 0 |

Google phục vụ cả 17 lượt, không fallback trong lượt cuối, 5 lượt được guard
chuẩn hoá. Case model sửa nhiệt độ step 1 thành 26°C và append `media.pause`
được thực hiện đủ 4 bước sau confirmation. Đây là development regression, không
phải chất lượng trên tập độc lập hoặc accuracy của model trước guard.

Giữ cả các kết quả thất bại để đối chiếu: initial run 3/4 episode do model đổi
exclusion thành positive target; guard recovery 4/4; lần context có 429 thì Google
phục vụ đúng 15 lượt, 2 lượt fallback chưa hoàn thành task. Lần đó cũng phát hiện
grant cũ có thể còn dùng sau câu sửa chưa hiểu, đã sửa và thêm regression.
Lượt cuối gọi cách nhau 4 giây để tránh rate limit; thời gian pacing không nằm
trong latency trên. Local thật chưa có phép đo; kiểm tra local dùng mock.

Các oracle cũ về “tăng điều hoà”/“một chút” được cập nhật theo quyết định mặc
định ±2°C mới; query log không thay đổi. Các báo cáo trước là kết quả theo chính
sách cũ. Ca thiếu target tuyệt đối vẫn kiểm tra clarification và giữ task.

Code đã sửa trong workspace. Backend cần reload để dùng prompt
`semantic-unified-8` và contract 7. Không gửi thao tác tới xe thật trong đợt test.
