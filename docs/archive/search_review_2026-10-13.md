# Review search ngày 13/10/2026

Đã review luồng parser/guard → InformationService → Tavily/Open-Meteo/Wikipedia → ConversationService → citation/API/UI. Các smoke test dùng câu hỏi công khai cố định, session/DB thử ở `/tmp`; không lấy GPS, memory hoặc lịch sử trong log người dùng để gửi API.

## Các lỗi đã sửa

- Câu hỏi tiếp “còn buổi tối”, “có chỗ phù hợp trẻ em”, “ở đó có chỗ ăn ngon” mất chủ đề search. Parser/guard nay giữ query chủ đề gốc qua nhiều lượt; địa danh mới rõ ràng thay chủ đề. Lệnh xe, navigation và câu hỏi thời tiết vẫn giữ routing riêng.
- Search không giải quyết “hôm nay/cuối tháng này” và generator thiếu ngày hiện tại. Query Tavily nay dùng ngày/tháng cụ thể theo timezone cấu hình, generator nhận cùng mốc ngày. Kiểm thử bao gồm đổi ngày giữa UTC/Việt Nam và giao năm/tháng.
- Ngày trong báo viết `18.10` bị hiểu thành số `1810`, nên model viết “18 tháng 10” bị báo số không được nguồn hỗ trợ. Chuẩn hóa ngày riêng theo ngữ cảnh lịch; vẫn giữ nguyên giá/số thập phân và chặn số tiền, ngày bị bịa.
- Model không có cách báo thiếu bằng chứng hợp lệ trong schema. Danh sách mã đoạn rỗng nay là quyết định từ chối trả lời; server bỏ text model và dùng câu thông báo cố định, giữ nguồn. Không tạo factual answer từ text không có hỗ trợ.
- Deadline tổng hợp làm mất nguồn đã tìm. Lượt quá hạn nay trả `information_timeout`, giữ citation và cache; thử lại dùng nguồn cache. HTTP/payload sai kiểu, nguồn rỗng và quota/offline vẫn trả lỗi có kiểu.
- Nguồn giá vàng được kiểm tra lại domain chính thức và ngày trọn vẹn trong excerpt. Bài có ngày đăng mới nhưng báo giá cũ không được dùng; metadata vẫn cho biết provider và số nguồn đã tìm.

## Smoke test API thật

| Kịch bản | Quan sát |
| --- | --- |
| “Ở Ninh Bình có gì chơi?” | Tổng hợp một câu gợi ý ba nơi, có citation; khoảng 3,9 giây. |
| “Còn buổi tối thì sao?” | Giữ Ninh Bình, tổng hợp hoạt động buổi tối và nguồn; khoảng 3,8 giây. |
| “Có chỗ nào phù hợp trẻ em không?” | Giữ Ninh Bình qua lượt hỏi tiếp thứ hai, trả gợi ý có nguồn; khoảng 3,2 giây. |
| “Hôm nay là ngày bao nhiêu âm lịch?” | Query và đáp án dùng ngày dương lịch 13/10/2026 theo Việt Nam, citation trỏ trang có ngày tương ứng; khoảng 2,9 giây. Không chạy bộ chuyển đổi âm lịch độc lập. |
| “Cuối tháng này có sự kiện gì vui ở Hà Nội?” | Nguồn tìm được chưa xác minh đúng khoảng cuối tháng; model trả mã đoạn rỗng, server thông báo thiếu bằng chứng và giữ liên kết, không repair; khoảng 2,2 giây. |
| “Tháng này có sự kiện gì vui ở Hà Nội?” với cùng nguồn đã lưu | Model tổng hợp lịch 16–18/10 trong một lần gọi; ngày `18.10` trong đoạn báo không còn gây `unsupported_numeric_claim`. Đây là kiểm tra lại bước tổng hợp, không gọi Tavily thêm. |
| “Giá vàng hôm nay” | Nguồn tìm được là các bài giá cũ; trả `current_quote_not_verified`, không gọi model để suy đoán giá. |
| “Thời tiết ngày mai tại Ninh Bình” → “địa điểm 1” | Geocoder trả nhiều địa danh trùng tên nên hỏi chọn; sau khi chọn candidate của server, trả dự báo cho 14/10/2026 từ Open-Meteo. |
| “Lịch sử Hoa Lư” | Trả một câu từ Wikipedia, có citation; khoảng 2,7 giây. |

Thời gian trên là quan sát từng lượt trên máy hiện tại, không phải benchmark p95 hoặc cam kết độ trễ. Lịch sự kiện và báo giá phụ thuộc nguồn được index; một lượt smoke không bảo đảm mọi câu tương tự luôn có dữ liệu.

## Regression

- 114 test search/open-world/source-synthesis đạt, gồm 54 ca trong `test_search_review.py`: hỏi tiếp, chuyển chủ đề, ngày/múi giờ, định dạng ngày/số tiền, source ID, abstention, lỗi payload, timeout/cached retry, provider fallback và domain/ngày giá vàng.
- 155 test dialogue, multitask, navigation follow-up, administrative destination và reset đạt.
- Frontend information tests, Ruff và `git diff --check` đạt.

Kiểm tra citation/số liệu chưa phải kiểm chứng ngữ nghĩa độc lập cho mọi khẳng định. Nguồn không có lịch đúng khoảng được hỏi hoặc báo giá đủ rõ phải dẫn đến thông báo chưa xác minh. Các liên kết tham khảo vẫn hiển thị trên UI; TTS đọc câu trả lời.

Tham số `basic`, `include_answer=false`, `include_published_date` và giới hạn domain đã đối chiếu [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search). Không đổi adapter hoặc tăng search depth/quota trong lượt review này.
