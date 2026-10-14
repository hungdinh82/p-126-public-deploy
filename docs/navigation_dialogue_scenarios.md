# Địa điểm, kế hoạch sạc và khởi hành

Semantic prompt `semantic-unified-19`; một LangGraph, cùng reducer/scheduler cho
cloud, local, parser và nút UI. Câu trả lời địa điểm có nghĩa rõ và lệnh tới
tỉnh/thành, tên thường gọi đã nhận diện hoặc câu chỉ đường có đích cụ thể dùng
parser trực tiếp để tránh chờ model. Model phân rã
các yêu cầu còn lại; guard không thay query bằng
regex. Guard/reducer kiểm tra handle, revision và quyền xác nhận. Parser fallback
vẫn có grammar tiếng Việt và xuất cùng contract.

## Câu hỏi đang chờ

| Tình huống | Hành vi |
|---|---|
| Đi Ninh Bình | VietMap xác minh phường Hoa Lư, tính nháp; hỏi xác nhận điểm đề xuất |
| OK / xác nhận / giữ địa điểm / có | Giữ điểm mặc định, chuyển sang bước tiếp theo; chưa chạy |
| Không / đổi địa điểm, khi đang hỏi điểm đề xuất | Hỏi tên địa điểm; chưa khởi hành |
| Tràng An | Tìm trong Ninh Bình; một kết quả hỏi “Đúng chỗ này chứ?” |
| Không, về Thanh Hoá | Đổi vùng, giữ loại chuyến đi; vô hiệu handle/revision trước |
| Nghi Sơn, Thanh Hoá | Tìm lớp hành chính và kiểm tra boundaries; không lấy đảo/khách sạn |
| Nhiều kết quả | Tối đa ba thẻ; chọn theo tên/số/handle hoặc bấm |
| Hồ Gươm / Hồ Tây / lăng Bác | Chuẩn hoá tên và khu vực từ `landmarks.json`; VietMap xác minh tọa độ, không nhận công ty/khách sạn chỉ trùng tên |
| Địa điểm chính và đường/tòa nhà phụ | Ưu tiên khớp tên chính; chỉ đề xuất trực tiếp khi bằng chứng tên, địa chỉ và khoảng cách đủ rõ |
| Không tìm được Đền Trần, rồi nói Nam Định | Giữ tên Đền Trần, tìm `Đền Trần, Nam Định`; không lấy điểm đại diện tỉnh |
| Hai trường/cơ sở/cổng cùng tên | Vẫn hỏi chọn đúng địa điểm; khớp tên không tự cấp quyền lái |
| Trung tâm không xác minh được | Hỏi địa điểm cụ thể; không dùng tâm tỉnh mới thay địa danh cũ |
| Huỷ tìm địa điểm / Huỷ yêu cầu | Huỷ các bước chờ; giữ hiệu ứng đã chạy và chuyến đang chạy |
| Lập kế hoạch rồi bật nhạc | Hoàn tất tính toán, tiếp tục bước nhạc; vẫn chưa khởi hành |
| Không, khi đang hỏi khởi hành | Giữ kế hoạch để xem lại |
| Đồng ý / bấm Bắt đầu | Chấp nhận đúng kế hoạch; kiểm tra lại vị trí/pin/policy, chạy một lần |
| Pin/vị trí/thông số thay đổi | Tính lại, tăng revision và hỏi xác nhận lại |
| Mở bản đồ | Chỉ xem; không gửi lệnh chạy hoặc thay tuyến đang chạy |

Mỗi lượt chỉ một câu hỏi. Câu trả lời ngắn lấy nghĩa theo
`clarification.phase`: `confirm_destination`, `specific`, `place_area`, hoặc danh sách ứng viên.
`place_area` giữ query POI đang tìm; câu trả lời khu vực bổ sung vào query đó.
Xác nhận điểm đề xuất bằng “OK”, “xác nhận”, “giữ địa điểm”; đổi điểm bằng
“đổi địa điểm” hoặc nói tên mới. “Không” từ chối điểm đang đề xuất.
Checkpoint cũ với `change_destination` vẫn giữ nghĩa “có” muốn đổi, “không” giữ điểm.
Tên/biệt danh trong `data/navigation/regions.json` chỉ là từ vựng/điểm cần xác minh;
VietMap cung cấp tọa độ và bằng chứng hành chính. Các query giữ dấu tiếng Việt.

## Hai trạng thái độc lập

- `navigation`: tuyến và tiến trình đang chạy/xem trước.
- `trip_plan`: ID/revision, `trip_kind`, điểm xuất phát/đích, lựa chọn đang chờ,
  tuyến/sạc, `calculation_status`, và đề nghị bắt đầu có kiểu.

`trip.plan`, `trip.refine`, `trip.select`, `trip.start`, `trip.decline`, `trip.cancel`
dùng các dịch vụ bản đồ/năng lượng chung. Không tạo graph thứ hai. `trip.plan`
hoàn tất khi trình bày kế hoạch; đề nghị khởi hành không giữ scheduler ở trạng thái
chờ confirmation của bước lập kế hoạch. Các typed action gửi qua endpoint turn
hiện có, mang `plan_id`, `route_revision` và handle nếu có. Response/stream/history
bổ sung `trip_plan`.

Khứ hồi: lấy tuyến lái riêng xuất phát → đích và đích → xuất phát ban đầu. Solver
mang SOC qua waypoint, không sạc hoặc cộng thời gian lưu lại tại đích. Cùng một
trạm có hai `visit_id`/`leg_index` riêng nếu ghé ở cả hai chiều. Giới hạn tính toán
hoặc thiếu đường về/trạm phù hợp tạo kết quả chưa hoàn chỉnh. Chỉ báo không cần
sạc khi đã kiểm tra toàn chuyến.

Trạm lấy từ VietMap. Thiếu thông số thiết bị dùng giả định đã được cho phép trong
`data/energy/charging-assumptions.json` (120 kW/CCS2, tối đa 80%); dữ liệu xác minh
được ưu tiên. Thời gian tính bằng đường cong xe và 5 phút thao tác, chưa tính chờ.

## Giao diện và migration

Màn hình xe luôn toàn chiều rộng; hội thoại và dock giữ nguyên vị trí. Địa điểm, kế hoạch, cảnh báo, hướng rẽ và bản đồ nhỏ nằm trong panel nhỏ bán trong suốt đè lên bên phải, không dành cột trống.
Màn hình nhỏ dùng panel giới hạn chiều cao, cuộn riêng và chừa khoảng cho mic/dock.
Ẩn/mở lại là trạng thái phiên theo request/plan ID, không huỷ task. Bản đồ giữ nút
và đầu trang ngoài phần chi tiết cuộn; xem kế hoạch không sửa bộ mô phỏng tuyến.

Migration runtime v5 sao lưu history, memory, ledger và checkpoint một lần, vô
hiệu grant cũ. Contract task v9 giữ bước đã hoàn tất; kế hoạch cũ không có loại
một chiều/khứ hồi bị chặn khi sử dụng lại, yêu cầu lập lại thay vì tự phát lệnh.
History thêm `trip_plan_json` tương thích; thêm cột trên DB có dữ liệu tạo backup
trước. Đợt thay đổi không restart backend hoặc sửa trực tiếp DB đang dùng.

## Ma trận chọn địa điểm (kiểm thử độc lập)

| Câu hỏi đang chờ | Người dùng trả lời | Chuyển trạng thái |
|---|---|---|
| Có đổi trung tâm không? | Không | Chấp nhận trung tâm, chưa chạy |
| Có đổi trung tâm không? | Có | Hỏi tên mới, chưa chạy |
| Một kết quả, đúng chỗ này? | Đúng / đồng ý | Chọn handle, xem trước tuyến |
| Một/nhiều kết quả | Không / không đúng chỗ này | Xoá ứng viên, tăng revision, hỏi tên mới; giữ các bước sau |
| Nhiều kết quả | Đồng ý | Hỏi chọn kết quả nào |
| Nhiều kết quả trùng tên | Tên chung | Hỏi số hoặc địa chỉ phân biệt |
| Danh sách có hai kết quả | Chỗ thứ ba / thứ 0 / thứ 9 | Báo số không có; không tìm kiếm câu trả lời này |
| Danh sách kết quả | Đổi sang Tràng An | Tìm lại; giữ yêu cầu nhiều bước |
| Đang hỏi tên mới | Tràng An | Tìm địa điểm; không chọn hay chạy ngay |
| Xác nhận khởi hành | Không | Giữ kế hoạch, chưa chạy |

`tests/test_destination_dialogue_scenarios.py` kiểm tra cùng câu trả lời cho cả
navigation/trip, và chuỗi từ chối → tên mới → danh sách. Các test trip/refinement
kiểm tra trung tâm, consent, stale handle và parser/cloud/local mock.
`eval/information_rail_browser.cjs` kiểm tra màn hình rộng/nhỏ: canvas luôn full,
các điều khiển không dịch khi hiện panel, ẩn/mở và cuộn trong bản đồ.

## Quyết định sau tìm kiếm — log 997–1002

Bước `destination_choice` gửi query và tối đa ba ứng viên có handle, tên, địa chỉ,
khoảng cách tới model đang phục vụ lượt. Model trả `propose` hoặc `clarify` cùng
handle/lý do. Điểm cụ thể khớp rõ hoặc yêu cầu gần nhất có thể được đề xuất thành
tuyến xem trước ngay. Nhiều chi nhánh/điểm vào hoặc bằng chứng thiếu thì hỏi chọn.
Quyết định không cấp quyền khởi hành. Timeout 3 giây, output sai hoặc không có
model quay về hỏi chọn; không tự chọn kết quả đầu. `selection_decision` lưu trong
state/history để kiểm tra cả quyết định model lẫn fallback.

Trước đó, gộp bản ghi có tên gốc tương thích, cùng địa chỉ và tọa độ cách tối đa
40 m. Hai chi nhánh xa nhau hoặc tên điểm vào khác vẫn giữ riêng. Không sửa tên
đầy đủ/địa chỉ vendor trong dữ liệu; TTS dùng tên ngắn đến phường.

Khi còn một ứng viên chưa chọn, "đúng, bắt đầu đi luôn" chọn handle trước, tính
kế hoạch và yêu cầu xác nhận tuyến. Không hỏi khởi hành khi chưa có tuyến. Mọi
thay đổi dựa trên đúng plan ID/revision. Các quy tắc này dùng chung trip/navigation;
`tests/test_destination_decisions.py` kiểm tra log trùng Ga Hải Phòng, model chọn
handle hợp lệ, output sai và đề xuất tuyến không làm xe chạy.
