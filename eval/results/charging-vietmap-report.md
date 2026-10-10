# Trạm sạc thật dọc tuyến: VietMap

## Hành vi

Runtime lấy POI trạm sạc và trạm dừng nghỉ trực tiếp từ VietMap. File
`data/energy/charging-stations.json` chỉ ghép thông số thiết bị có nguồn; không
thay vị trí bản đồ, không dùng synthetic nếu API lỗi. Xe/pin vẫn dùng hồ sơ VF8
Plus gần đúng người dùng đã chọn; nguồn địa điểm độc lập với hồ sơ năng lượng.

Discovery tìm theo vòng tròn ở tối đa 6 điểm trên route, giới hạn POI; hydrate
tọa độ, loại trạm xe máy/tủ đổi pin/không phải trạm sạc và POI ngoài hành lang.
Kiểm tra cả đường lái tới trạm và từ trạm tới đích; giới hạn quãng đường vòng
15 km, không dùng khoảng cách thẳng để tính pin. Lấy đoạn đường từ Route và
tên trạm dừng nghỉ từ POI thật gần trạm sạc (≤300m). Không suy số trạm hoặc
cột km cao tốc. Rest-area association được đọc là “gần”, không cam kết cùng
khuôn viên hoặc cùng chiều đường khi chưa có dữ kiện đó.

Khi thiếu công suất/đầu sạc, `station_data_required` hiển thị các trạm thật có
thể tới nhưng chưa cấp grant cho toàn itinerary sạc. “Đến trạm một” hoặc nút
UI dùng handle station_candidates, tính tuyến tới trạm và chờ xác nhận. Trạm
có thông số phù hợp được dùng trong bộ tính phút sạc; checkpoint giữ thông số
để tiếp tục sau restart. Pending itinerary cũ dùng trạm synthetic/không có map
ref bị chặn khởi hành và yêu cầu tính lại.

Các marker/list trạm lấy từ server. Đổi đích loại marker cũ; response revision
cũ không phục hồi lại. Context model giữ handle, forecast và thông tin POI,
loại polyline/legs; tuyến thật kiểm tra có 1.434 điểm, gần 40 KB JSON, chỉ cần
giữ trong checkpoint/UI. Semantic prompt cập nhật `semantic-unified-10`.

## Smoke API thật

[Kết quả JSON](charging-vietmap-hanoi-thanhhoa.json) được lấy bằng
`python -m eval.charging_map_smoke --soc 60`, vị trí cấu hình VinUni, điểm đích
Vincom Plaza Thanh Hóa. Đây là kiểm tra read-only với SOC nhập cho smoke;
không dùng pin thật, không khởi hành, không ghi DB hội thoại.

- Route API trả quãng đường khoảng 172 km.
- Tìm được **Trạm Sạc Điện Ô Tô VinFast Trạm Dừng Nghỉ Ninh Bình**,
  liên kết POI gần đó **Trạm Dừng Nghỉ Xuân Khiêm**.
- Đoạn đường gần POI: **CT. Cao Bồ Mai Sơn**, từ Route API.
- Quãng đường lái tới trạm khoảng **102,6 km**, vòng thêm khoảng **108 m**.
- Với SOC kiểm thử 60%, hồ sơ VF8 Plus gần đúng: đến trạm dự kiến **30,4%**.
- Map trả tên, địa chỉ và tọa độ; không có công suất/connector. Không sinh số
  phút sạc hay xác nhận toàn itinerary sạc trong ca này. Trụ trống/giờ hoạt
  động vẫn chưa biết. Không khẳng định xe đủ/thiếu pin khi chưa có telemetry.

API tham khảo:
[Autocomplete v4](https://maps.vietmap.vn/docs/map-api/autocomplete-version/autocomplete-v4/)
(`circle_center`, `circle_radius`, `layers`) và
[Place v4](https://maps.vietmap.vn/docs/map-api/place-v4/) (chi tiết POI/toạ độ).
Kho thiết bị rỗng không cản việc tìm/đề xuất trạm thật; chỉ còn thiếu dữ liệu
công suất trụ có nguồn để hoàn tất kế hoạch với số phút sạc.

## Kiểm thử

- Full regression: **533 passed**, 275,53 giây, bao gồm API/parser, MQTT với
  broker test, confirmation, checkpoint, memory/RAG và navigation.
- Frontend: **17 passed**, gồm marker trạm/POI source, chọn có kiểu, giữ xác
  nhận, response cũ, barge-in và streaming TTS.
- Bộ mới kiểm tra bán kính/layers API, dedup/hydrate, nguồn công suất và
  metadata synthetic bị loại, route projection, liên kết trạm dừng nghỉ thật,
  sai chiều/vòng quá xa, thiếu đường, pin không tới được, connector không phù
  hợp, chọn trạm bằng lời và restart/replay không tìm lại hoặc chạy lại lệnh.
- Sau hai bổ sung cuối (context model và chặn pending synthetic), regression
  cho các module thay đổi: **140 passed**, 92,42 giây. Có ca riêng kiểm tra
  context không đổi checkpoint và chặn khởi hành itinerary synthetic cũ.
- Ruff và `git diff --check` được kiểm tra riêng. Không đánh giá accuracy cloud
  thật từ điểm số fixture; smoke bản đồ chỉ xác minh API/road/POI integration.
