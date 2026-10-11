# Hồ sơ năng lượng ViVi

Backend mặc định nạp `vf8-plus.json`: VF8 Plus bản pin khả dụng 87,7 kWh,
giới hạn DC 149 kW theo [VinFast](https://vinfastauto.com/vn_vi/dat-coc-xe-vf8).
Không áp hồ sơ này cho VF8 thế hệ mới 60,13 kWh. Người dùng đã chọn thông số
gần đúng: tiêu hao 22 kWh/100 km và đường cong sạc là giả định, có provenance
từng trường; toàn bộ hồ sơ được đánh dấu `simulated=true`.

Hệ số 1,15 cho tiêu hao dự phòng 25,3 kWh/100 km. Với tuyến fixture 120 km,
tiêu thụ 30,36 kWh, tương đương 34,62 điểm SOC: pin 70% đến còn khoảng 35,38%;
pin 40% đến còn 5,38% nên cần kế hoạch sạc đạt dự phòng 15%.

Vị trí và tên trạm được tìm **trực tiếp từ VietMap dọc tuyến**, không lấy từ
file giả lập. Bộ tính kiểm tra đường lái tới trạm và từ trạm về đích, loại điểm
vòng quá xa/sai chiều, giữ dự phòng 15%. POI trạm dừng nghỉ gần trạm sạc được
liên kết bằng tọa độ thật; tên đoạn đường lấy từ Route API. Không tự tạo số
trạm hoặc cột km cao tốc.

`charging-stations.json` chỉ bổ sung **thông số trụ đã xác minh** cho POI trên
bản đồ, đang rỗng. Mỗi record có schema:

```json
{
  "id": "station-verified-id",
  "map_ref_id": "ref_id nhận từ VietMap",
  "name": "Tên trạm đã xác minh",
  "lat": 21.0,
  "lng": 106.2,
  "network": "vgreen",
  "connector": "CCS2",
  "power_kw": null,
  "max_soc": 80,
  "accessible": true,
  "source": "URL/tài liệu xác minh trạm",
  "updated_at": "2026-10-10",
  "simulated": false
}
```

Toạ độ trong ví dụ chỉ minh hoạ schema. Điền toạ độ thật và công suất đúng trụ.
Metadata chỉ được ghép khi ref_id trùng và tọa độ cách ≤100m; record cũ chưa
có ref_id cần khớp duy nhất cả tên lẫn tọa độ. Bản ghi simulated không được ghép.
Thông số thiếu không được gắn nhãn xác minh. Theo
[V-Green](https://vgreen.net/vi/tram-sac-o-to-dien), trụ ≥250 kW giới hạn SOC 80%;
validator áp giới hạn này kể cả file nhập có max_soc cao hơn.

Thử bằng bản đồ thật (chỉ đọc API, không tác động xe hoặc history):

```bash
.venv/bin/python -m eval.charging_map_smoke --soc 60 \
  --output /tmp/vivi-charging-map.json
```

`charging-assumptions.json` mặc định bổ sung giả định **120 kW, CCS2, SOC tối đa
80%** cho POI ô tô VinFast/V-Green thật chưa có thiết bị xác minh. Nguồn vị trí
vẫn là VietMap; `equipment_basis=assumed` phân biệt công suất giả định với dữ liệu
xác minh. Thông số xác minh được ưu tiên. Đặt `CHARGING_ASSUMPTIONS_ENABLED=false`
để không dùng giả định. Bộ tính giới hạn theo công suất xe và đường cong sạc,
cộng 5 phút thao tác mỗi lần dừng. Không tính thời gian chờ trụ.

Kho simulated còn được giữ cho fixture kiểm thử, không phải trạm thực tế và
không phải fallback của runtime khi API bản đồ lỗi. Không lấy khoảng cách thẳng
để tính pin. Khi không có thông số/giả định hợp lệ, vẫn hiển thị trạm thật có thể tới;
chưa cấp grant cho toàn tuyến sạc. “Đến trạm một” hoặc nút Dẫn tới trạm chọn
POI qua cùng scheduler, tính tuyến tới trạm rồi chờ xác nhận khởi hành.
Để tính phút sạc cần bổ sung thông số trụ xác minh, vị trí/GPS và SOC telemetry có thời
điểm đo, hiệu chỉnh tiêu hao và đường cong sạc theo đúng xe. Trụ trống/thời gian
chờ chưa có nguồn; giao diện giữ trạng thái chưa biết. Reload backend sau đổi file.
