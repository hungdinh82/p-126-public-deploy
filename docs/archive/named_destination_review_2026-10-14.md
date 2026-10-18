# Địa điểm cụ thể — review log 2026-10-14

Nguồn: `data/2026-10-14/turns.jsonl`, `handbook_turns` trong SQLite read-only và các lượt tối 13/10 liên quan. Thời gian dưới đây là giờ Việt Nam (UTC+7).

## Những lỗi thấy trong log

| Lượt / giờ | Yêu cầu | Kết quả trước sửa |
| --- | --- | --- |
| 1275 / 08:29:55 | Chỉ đường tới Hồ Gươm | Trả trung tâm thương mại và các công ty có tên Hồ Gươm; 11,10 s |
| 1277 / 08:30:38 | Chỉ đường tới đền Trần | Không tìm được kết quả phù hợp; câu hỏi không giữ riêng tên POI để bổ sung khu vực; 10,81 s |
| 1278 / 08:30:57 | Chỉ đường tới hồ Hoàn Kiếm | Hỏi chọn giữa hồ và Phố Hồ Hoàn Kiếm |
| 1279 / 08:32:01 | Chỉ đường tới quảng trường Ba Đình | Hỏi chọn giữa tên chính và bản ghi có thêm Hùng Vương; 10,94 s |
| 1280 / 08:32:42 | Chỉ đường tới Vinhomes Times City | Hỏi chọn giữa khu đô thị và các tòa/địa chỉ bên trong; 10,86 s |
| 1281 / 08:33:51 | Chỉ đường tới tiểu học Võ Thị Sáu | Có các cơ sở khác nhau, gồm kết quả ở tỉnh/thành khác; trường hợp này thực sự cần chọn |
| 1286 / 08:36:06 | Tôi muốn đến Hồ Gươm | `manual.search`, trả nội dung cẩm nang về thẻ sạc; 12,05 s |
| 1287 / 08:36:23 | Tôi muốn đến Hồ Tây | `manual.search`, trả lại nội dung thẻ sạc |
| 1288 / 08:36:58 | Tôi muốn đến lăng Bác | `manual.search`, trả nội dung đèn soi bậc cửa; 10,54 s |

“Chở tôi tới Hồ Gươm” tối 13/10 cũng chưa được grammar fallback hỗ trợ. Sáng 14/10 có transcript “Chào tôi tới Hồ Gươm”; chưa đối chiếu âm thanh nên không kết luận chắc đây là lỗi STT. Không tự coi câu mơ hồ này là lệnh lái.

Google liên tục timeout khoảng 10 s, local tiếp tục lỗi kết nối. Vì vậy các lỗi grammar trở nên rõ khi phải dùng rules. IO timings mới ở các lượt này chủ yếu hàng chục đến hàng trăm ms; chúng không giải thích thời gian chờ Google. Lỗi nhỏ POI khác với lỗi xác nhận điểm đại diện tỉnh/thành ở review trước.

Lưu ý đọc log: kế hoạch cũ tới Hỏa Lò nằm trong `trip_plan`, còn câu hỏi đền Trần thuộc `navigation`. Ở turn chính thức của đền Trần, danh sách navigation đã rỗng. Không được lấy danh sách trip cũ để kết luận navigation đang chọn Hỏa Lò. Regression mới vẫn kiểm tra việc tìm đích mới thất bại khi một chuyến khác đang chạy.

## Sửa trong code

- Grammar nhận “tôi/mình muốn/cần đến/tới/đi/về…” và “chở/đưa tôi/mình…”. Các query giữ tiếng Việt và bỏ phần yêu cầu/đuôi lịch sự; không gửi câu này vào RAG chỉ vì có từ “đến”.
- `SemanticResolver` dùng đường trực tiếp cho lệnh một mệnh đề có đích cụ thể rõ, các vùng và landmark đã nhận diện. Câu thiếu đích, địa điểm chung như “quán cà phê”, hẹn giờ/điều kiện và nhiều thao tác không bị shortcut bỏ mất ý nghĩa. Prompt phiên bản `semantic-unified-19`.
- `data/navigation/landmarks.json` chỉ có ba mục Hồ Hoàn Kiếm/Hồ Gươm, Hồ Tây và Lăng Chủ tịch Hồ Chí Minh/lăng Bác. Danh mục chứa tên và khu vực, không chứa tọa độ. Khi tên/khu vực khớp toàn bộ, search dùng tên chuẩn và Hà Nội; tên doanh nghiệp như “Hồ Gươm Plaza” hoặc khu vực khác người dùng nêu không bị viết lại.
- Search kiểm tra khu vực đã nêu trong query, lọc landmark theo tên, ưu tiên tên chính trước khi giới hạn số ứng viên. Doanh nghiệp chỉ chứa tên landmark và địa chỉ trong cùng khu vực không trở thành landmark đó.
- `exact_destination` có thể đề xuất tuyến cho một kết quả khớp tên duy nhất khi những kết quả khác chỉ là đường/bản ghi phụ gần đó, cùng địa chỉ. Có giới hạn khoảng cách và chặn cơ sở/cổng/lối vào khác; hai kết quả cùng tên hoặc địa chỉ khác vẫn hỏi lại. Một landmark được bản đồ xác minh chính xác có thể đi thẳng tới preview. Các tên trường trùng nhau tiếp tục chọn theo danh sách/handle.
- Khi không có POI phù hợp, `place_area` giữ `clarification.query`. “Đền Trần” + “Nam Định” thành `Đền Trần, Nam Định`; không đổi thành tâm tỉnh. Parser trực tiếp, resolver, trip adapter và semantic prompt dùng cùng contract.
- Với chuyến đang chạy, tìm đích khác thất bại giữ route hiện hành và công bố danh sách mới rỗng, thay vì giữ ứng viên của chuyến cũ trong câu hỏi mới. Việc xem trước hoặc chọn POI chưa khởi hành; bước chấp nhận tuyến vẫn riêng.

## Kiểm chứng và giới hạn

27 regression cases mới dùng tên/kiểu lỗi trong log, tọa độ và đường đi giả lập. Bao phủ các câu tự nhiên, alias không ghi đè doanh nghiệp/khu vực khác, bỏ thời gian chờ model với lệnh rõ, điểm chính so với kết quả phụ, trường/cổng/cơ sở trùng, bổ sung khu vực cho navigation và trip, giữ chuyến đang chạy khi tìm đích mới thất bại, cùng hẹn giờ/multi-action/STT mơ hồ. 172 tests tập trung đã pass.

Replay riêng hàm quyết định trên ứng viên đã ghi trong log, hoàn toàn cục bộ: chọn Hồ Hoàn Kiếm từ hai kết quả, Quảng Trường Ba Đình từ hai kết quả và Vinhomes Times City từ ba kết quả; ba trường Võ Thị Sáu vẫn yêu cầu phân biệt. Không gọi lại nhà cung cấp cho replay này.

Quality gate `bash scripts/check_local.sh` đã pass: whitespace, Ruff, frontend syntax và **966 Python tests** trong 308,71 s, gồm MQTT và concurrency. Kiểm tra cú pháp `journey.js` cũng pass.

Không gửi lịch sử hay GPS riêng tư cho nhà cung cấp trong đợt này. Kiểm thử không khẳng định VietMap luôn có kết quả cho mọi POI, cũng không đo chất lượng STT trên audio thật. Cần khởi động lại backend và kiểm tra log mới với các câu đã sửa; nếu bản đồ không có tên, Vivi sẽ hỏi khu vực/địa chỉ để tìm tiếp.
