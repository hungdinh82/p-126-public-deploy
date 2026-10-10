# Rút gọn lời đọc dẫn đường — log 959–966

## Nguyên nhân

- Log 962: `navigation.search` và `navigation.plan` dùng cùng danh sách ba
  handle. Backend ghép cả hai message vào response, khiến danh sách được đọc
  hai lần ngay trong cùng câu trả lời. Đây là lỗi tổng hợp kết quả đa bước.
- Log 963–964: preview đọc tên POI chứa tên đường, phần thành phố, SOC hai chữ
  số thập phân và nhãn mô phỏng. Nhấn Bắt đầu tạo lại preview đầy đủ.
- Log 965 và lời nhắc đến nơi cũng có nội dung có thể rút gọn.

## Thay đổi

- Tầng presentation dựng tên ngắn kèm phường/xã; không đọc thành phố và địa chỉ
  tự do. Nếu các chi nhánh trùng tên ngắn, giữ tên đường để phân biệt.
- Tổng hợp response chỉ giữ lần đọc cuối của cùng danh sách handle; giữ các
  kết quả điều hoà và các external effects khác. Không deduplicate bằng nội dung
  tiếng Việt. Step results, địa chỉ đầy đủ và route không bị xoá.
- SOC đọc phần trăm nguyên, làm tròn xuống; phút sạc làm tròn lên. Giữ lời
  “dự kiến/khoảng”, bỏ câu nhãn mô phỏng của pin/sạc. Metadata và số liệu đầy đủ
  vẫn có trong navigation state/UI.
- Bắt đầu trên preview hiện có chỉ hỏi xác nhận ngắn; vẫn cấp grant mới và chờ
  đồng ý. Khởi hành báo “Đã bắt đầu dẫn đường.”. Lời đến nơi dùng `spoken_label`
  từ server, cùng quy tắc rút gọn; bản đồ tiếp tục dùng tên đầy đủ.
- Frontend phân biệt lời báo chờ với audio trả lời thực sự đã xếp hàng. Các
  speech segments không được phát lại qua final; browser fallback đọc final
  một lần, và audio cũ bị chặn khi audio epoch thay đổi. Bump cache app/journey.

## Kiểm chứng

- 134 regression backend pass: presentation mới, navigation workflow, audit
  Oct10, đa bước và unified dialogue. Cloud/local dùng mock semantic output;
  parser chạy thực. Không gọi provider hay VietMap thật.
- Sau thay đổi cuối về xác nhận ngắn: chạy lại 36 regression navigation và
  presentation, tất cả pass (23.17 giây).
- 15 frontend tests pass, chạy từng file bằng `node`: barge-in, confirmation,
  navigation và workflow/TTS. Có ca arrival label, streaming/final, progress-only,
  browser voice fallback và audio epoch.
- Ruff trên Python thay đổi và `git diff --check` pass.
- Test dùng SQLite tạm, VehicleSimulator và road/energy fixtures; không sửa
  history người dùng, không gửi MQTT hay thao tác trên xe thật. Chưa kiểm thử
  âm thanh thực trên trình duyệt/thiết bị trong đợt này.
