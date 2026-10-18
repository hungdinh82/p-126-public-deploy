# Xác nhận điểm mặc định và độ trễ — 2026-10-13

## Log được kiểm tra

Nguồn: `data/2026-10-13/turns.jsonl` và bảng `handbook_turns` trong SQLite, đọc ở chế độ read-only. Thời gian dưới đây là giờ Việt Nam (UTC+7). Tổng thời gian lấy từ JSONL, vì `timings_json` trong lịch sử chỉ lưu các span của graph.

| Lượt / giờ | Câu nói | Kết quả trước sửa | Tổng thời gian |
| --- | --- | --- | --- |
| 1256 / 20:02:26 | Lập lộ trình đi và sạc tới Quảng Ngãi | Đề xuất điểm đại diện, hỏi muốn đổi không | 16,73 s |
| 1257 / 20:03:02 | OK | Chọn nhánh đổi điểm, hỏi tên mới | 14,86 s |
| 1258 / 20:03:26 | Đến trung tâm tỉnh Quảng Ngãi luôn | Tìm nguyên tên kèm “luôn.”, không có kết quả | 10,78 s |
| 1259 / 20:04:04 | Chỉ đường tới Quảng Ngãi | Đề xuất lại điểm đại diện | 26,99 s |
| 1260 / 20:04:46 | Giữ địa điểm | Tìm “Giữ địa điểm.” như tên địa danh | 22,42 s |

Tên Quảng Ngãi đã được nhận diện và bản nháp tuyến đã có. Lỗi chính là phân loại câu trả lời sau đề xuất: câu hỏi phủ định `change_destination` dùng “có” để đổi, nhưng “OK” cũng bị ánh xạ vào nhánh đó. Parser chưa nhận “giữ địa điểm”, và chưa bỏ đuôi lịch sự trong tên được gửi sang bản đồ.

Lượt 1252 trước đó có hai action `navigation.plan` và `trip.plan` cho cùng câu “chỉ đường và lập lộ trình sạc tới Huế”. Grammar mới nhận cả cụm này thành một `trip.plan`.

## Nguyên nhân chậm

- Google trả HTTP 503 trong khoảng 1,6 s ở hai lượt đầu. Các lượt 1259 và 1260 hết hạn sau khoảng 10–10,5 s. Local fallback tiếp tục lỗi kết nối, rồi mới dùng rules. Không cần chờ model để giải nghĩa “OK” hay “giữ địa điểm” khi đã có câu hỏi và handle rõ ràng.
- Lập tuyến/sạc thực sự tốn thời gian: capability khoảng 8,27–9,20 s, discovery khoảng 3,12–3,50 s. `navigation_route` cộng thời gian của nhiều lời gọi có thể chạy song song, nên không được cộng nó vào capability hay coi là một thời gian chờ độc lập.
- Ở lượt “OK”, tổng 14,86 s trong khi các node graph đo khoảng 7,52 s. Khoảng 7,34 s còn lại nằm ngoài các span đó. Streaming dựng lại trạng thái có geometry cho cả các update không được chat dùng; các `values` trung gian còn mang output của lượt trước cũng bị project lại. Có thêm chi phí lưu/checkpoint và copy/serialize trạng thái lớn. Log cũ chưa đủ để quy toàn bộ phần dư cho SQLite hay một hàm cụ thể.

## Thay đổi

1. Câu hỏi mới dùng `confirm_destination`: “Mình đề xuất … Bạn xác nhận dùng điểm này nhé, hoặc nói địa điểm muốn đổi.” “OK”, “xác nhận”, “giữ địa điểm”, “có” giữ điểm; “không”, “đổi địa điểm” hỏi tên mới. UI và typed yes/no dùng cùng nghĩa. Việc xác nhận điểm chỉ hoàn tất kế hoạch/xem trước; đề nghị khởi hành vẫn riêng.
2. Checkpoint cũ `change_destination` giữ nghĩa literal “có” đổi / “không” giữ. “OK”, “xác nhận”, “giữ địa điểm” ở checkpoint cũ giữ điểm. Handle/revision và quyền sở hữu câu hỏi vẫn được kiểm tra; một câu xác nhận của thao tác xe không chuyển thành xác nhận địa điểm.
3. `SemanticResolver` giải quyết câu trả lời rõ ràng theo lựa chọn hiện tại trước khi gọi cloud/local. Lệnh một mệnh đề tới tỉnh/thành đã nhận diện cũng dùng parser trực tiếp; các câu thiếu đích, phủ định, có điều kiện, hẹn giờ hoặc nhiều thao tác vẫn đi qua luồng bình thường. Không biến mọi query bản đồ thành rule.
4. Bỏ dấu câu và đuôi “luôn”, “nhé”, “giúp tôi”… trước khi gửi tên địa điểm. Câu “đến trung tâm tỉnh … luôn” có thể nhận lại đúng lựa chọn đang đề xuất mà không search lần nữa.
5. Orchestrator dùng `astream_dialogue`: chỉ hydrate các update domain mà nó sử dụng để phát navigation/trip event; kết quả cuối và update bản đồ vẫn có geometry. Output thuộc lượt trước không được project lại ở mỗi node của lượt mới. Public projection dùng một lần dump session, thay vì dump → validate → dump.
6. Adapter đọc trip trong parser không deep-copy toàn bộ session, bao gồm cả hành trình khác đang chạy. Log bổ sung `diagnostics.io_timings.checkpoint` và `navigation_store`, mỗi mục có số lần và tổng thời gian await. Đây là các span chẩn đoán có thể chồng lấn; không cộng vào tổng wall time.

## Kiểm chứng

25 regression cases mới và 98 tests tập trung đã pass: xác nhận giữ điểm không gọi model/search/route lại và không lái; đổi điểm; polarity mới/cũ; typed yes/no; revision cũ; quyền sở hữu của thao tác xe; grammar một yêu cầu lập tuyến/sạc; streaming giữ geometry sau hai lượt; số lần hydrate giảm; IO timings được ghi cả invoke và stream.

Quality gate `bash scripts/check_local.sh` đã pass: whitespace, Ruff, frontend syntax, **939 Python tests** trong 379,90 s, gồm các test MQTT và concurrency. `node --check journey.js` cũng pass.

Đo riêng streaming trên tuyến giả lập 5.656 điểm, không gọi dịch vụ ngoài: lượt lập kế hoạch giảm từ 28 xuống 22 lần hydrate, lượt giữ điểm từ 27 xuống 21. Đây là so sánh public streaming cũ với streaming chat mới trên cùng logic đã sửa. Các mẫu wall time chạy cùng lúc với quality gate dao động nhiều, nên không dùng để cam kết độ trễ thực tế.

Cần khởi động lại backend và lấy log mới để đo phần còn lại, đặc biệt thời gian chờ model ở các câu phức tạp, thời gian bản đồ và IO. Không replay GPS/lịch sử riêng tư tới dịch vụ ngoài trong đợt kiểm tra này.
