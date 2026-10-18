# Jetson và Gemini

Cùng input và oracle nghiệp vụ; raw output nằm trong thư mục mỗi agent.

| Tính năng | Ca mỗi agent | Jetson pass | Gemini pass |
|---|---:|---:|---:|
| doors | 94 | 92 | 92 |
| windows | 56 | 56 | 56 |
| music | 23 | 22 | 23 |
| climate | 60 | 59 | 60 |
| body_panels | 70 | 69 | 70 |
| vehicle_status | 144 | 129 | 137 |
| navigation | 153 | 104 | 137 |
| information | 54 | 49 | 51 |
| memory | 40 | 21 | 34 |
| dialogue | 30 | 25 | 28 |
| hard_cases | 34 | 28 | 33 |
| multi_tool | 50 | 41 | 50 |
| adversarial | 26 | 24 | 25 |

[Chi tiết Jetson](jetson/index.md) · [Chi tiết Gemini](gemini/index.md)

---

## Tổng hợp và đánh giá

Lượt chạy ngày 2026-10-18, revision `9f52179` (working tree có 2 thay đổi chưa commit: `src/vivi/dialogue/planner.py`, `benchmarks/agent/runtime.py`). Cả hai agent dùng cùng corpus 834 ca live (732 ngôn ngữ, 102 agent/xe mô phỏng), cùng oracle và cùng bộ small tools (`local-operations-7`). Khác nhau về transport: Jetson dùng JSON grammar, Gemini dùng native function calling.

| | Jetson (`vivi-qwen3-1.7b`) | Gemini (`gemini-3.5-flash-lite`) |
|---|---|---|
| **Tổng pass** | **719/834 (86,2%)** | **796/834 (95,4%)** |
| Suite ngôn ngữ (732 ca) | 620 | 694 |
| Suite agent/xe mô phỏng (102 ca) | 99 | 102 |
| Latency model p50 / p95 | 2,3s / 4,2s | 1,2s / 4,7s |
| Repair / fallback | 13 / 5 | 4 / 0 |

Cả hai cùng pass 718 ca; chỉ Gemini pass 78 ca; chỉ Jetson pass 1 ca; cả hai cùng fail 37 ca.

### Cách chạy và dữ liệu

- Jetson chạy một lượt liền 834/834 ca, không lỗi dịch vụ.
- Gemini chạy qua nhiều phiên (`--resume`) vì key cũ bị giới hạn (HTTP 429); các ca 429 được chạy lại tự động. Đổi sang key mới ở giữa lượt.
- Lượt đầu của Gemini có **20 ca rơi vào fallback** do hạ tầng: 15 ca hết ngân sách thời gian 10 giây và 5 ca lỗi HTTP 503. Các ca này bị tính fail dù không phải lỗi hiểu ngôn ngữ. Cả 20 ca đã được chạy lại (concurrency 1, cùng source/dataset/model): **20/20 pass, không còn fallback**. Điểm Gemini tăng từ 776 lên 796. Latency của 20 ca chạy lại không so sánh trực tiếp với phần còn lại.
- Bản gốc trước khi thay 20 ca không được giữ trong thư mục này; thay đổi chỉ là thay các dòng tương ứng trong `gemini/progress.jsonl` bằng kết quả chạy lại.

### Đánh giá

1. **Điều khiển xe đơn giản (cửa, kính, nhạc, điều hòa, capo/cốp):** Jetson ngang Gemini hoặc chỉ kém 0–1 ca. Đây là phần mà bộ small tools phục vụ SLM tốt.
2. **Khoảng cách nằm ở navigation, memory và vehicle_status:** navigation 104/153 so với 137/153, memory 21/40 so với 34/40, vehicle_status 129/144 so với 137/144. Các nhóm khác chênh 0–3 ca (multi_tool chênh 9 ca).
3. **Lỗi hạ tầng từng che mất năng lực thật của Gemini.** Trước khi chạy lại, doors, windows, information của Gemini thấp hơn Jetson; sau khi loại lỗi timeout/503, Gemini ngang hoặc hơn Jetson ở mọi nhóm.
4. **Lỗi thường gặp của Jetson:**
   - Memory: câu "nhớ rằng tôi tên Minh" bị hỏi lại thay vì ghi nhớ; "quên tên tôi", "xóa ghi nhớ" bị gọi sai tool hoặc không đúng khóa `display_name`.
   - Navigation: câu có điểm đi và điểm đến ("chỉ đường từ Hà Nội đến Tràng An"), "sửa điểm đến thành …", "chưa đi theo tuyến này", "kết thúc dẫn đường".
5. **Gemini còn 38 ca fail thật** (phần lớn trong navigation 16, vehicle_status 7, memory 6); ví dụ lỗi gõ `khóaf cửa bên tài nhe` bị hiểu sai.
6. **37 ca cả hai cùng fail** là ứng viên đáng xem lại: oracle có thể chưa khớp, hoặc cả hai model cùng hiểu sai.
7. **Tốc độ:** ngoài các ca timeout, Gemini nhanh hơn Jetson (p50 1,2s so với 2,3s).

### Lưu ý khi đọc

- Đây là corpus development, chưa phải holdout được review độc lập; bộ test và bộ tool được thiết kế cùng nhau nên điểm có thể cao hơn thực tế.
- Mỗi ca chạy 1 lần, chưa lặp; chênh vài ca là nhiễu.
- Oracle chấm theo quyết định nghiệp vụ và tham số, không theo tên alias riêng của từng model.
- Gemini được chạy bằng bộ tool mới nhờ bỏ điều kiện `provider.name == "local"` trong `planner.py`; cấu hình production của Gemini vẫn là `legacy`.
