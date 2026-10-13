# Kịch bản UAT toàn bộ luồng ViVi

Tài liệu này dành cho người kiểm thử chưa từng dùng ViVi. Hãy thực hiện theo thứ tự, ghi `PASS`, `FAIL`, `BLOCKED` hoặc `N/A` cho từng ca và đính kèm ảnh/video khi có lỗi.

> ViVi hiện là prototype chạy với xe mô phỏng, không phải sản phẩm VinFast chính thức và không được dùng để điều khiển xe thật.

## 1. Mục tiêu và phạm vi

Đợt UAT kiểm tra bốn khu vực:

1. Chọn vai trò và truy cập đúng màn hình.
2. Không gian Tài xế: hội thoại, điều khiển cabin, xác nhận an toàn, cẩm nang, bộ nhớ, giọng nói và hành trình.
3. Edge Monitor: tài nguyên, trạng thái pipeline, lịch sử lượt và reset lịch sử.
4. Engineer Dashboard: runtime, Digital Twin, scenario, model profile, OTA mô phỏng và audit.

Không đánh giá độ chính xác của xe thật, GPS thật, giọng nói trong môi trường ồn hoặc chất lượng của dịch vụ cloud nếu môi trường test không được cấu hình các thành phần đó.

## 2. Quy ước kết quả

| Kết quả | Khi nào dùng |
| --- | --- |
| `PASS` | Thao tác và kết quả thực tế đúng với mong đợi. |
| `FAIL` | Có thể thực hiện nhưng kết quả sai, thiếu phản hồi, treo hoặc hiển thị lỗi. |
| `BLOCKED` | Không thể test vì thiếu cấu hình, thiết bị, quyền microphone, API key hoặc dịch vụ ngoài. |
| `N/A` | Điều phối viên xác nhận ca này không thuộc cấu hình cần nghiệm thu. |

Mức độ lỗi đề xuất:

- `P0`: mất dữ liệu nghiêm trọng, rủi ro an toàn hoặc toàn bộ app không dùng được.
- `P1`: luồng chính không hoàn thành, báo thành công sai hoặc policy an toàn bị vượt qua.
- `P2`: một chức năng quan trọng sai nhưng còn cách làm khác.
- `P3`: lỗi hiển thị, câu chữ hoặc trải nghiệm không chặn công việc.

## 3. Chuẩn bị trước khi giao cho tester

Phần này do người vận hành môi trường thực hiện. Tester chỉ cần nhận URL và cấu hình nào đang bật.

### 3.1 Khởi động nhanh

Tại thư mục dự án:

```bash
bash scripts/setup.sh
.venv/bin/python run.py
```

Mở `http://127.0.0.1:8787`. Nếu test từ máy khác, điều phối viên cung cấp URL HTTPS/ngrok tương ứng.

Profile ổn định, không cần microphone hoặc dịch vụ AI ngoài:

```toml
stt_provider = "off"
llm_provider = "rules"
tts_provider = "off"
rag_retrieval_mode = "sqlite"
vehicle_provider = "memory"
```

Sau khi đổi `config.toml`, phải khởi động lại backend.

### 3.2 Thông tin điều phối viên phải điền

| Mục | Giá trị |
| --- | --- |
| Phiên bản/commit |  |
| URL test |  |
| Trình duyệt và phiên bản |  |
| Hệ điều hành/thiết bị |  |
| STT provider |  |
| LLM provider |  |
| TTS provider |  |
| Vehicle provider (`memory`/`mqtt`) |  |
| VietMap đã cấu hình? | Có / Không |
| Ngày, giờ test |  |
| Người test |  |

### 3.3 Dữ liệu và điều kiện ban đầu

- Dùng cửa sổ trình duyệt mới; cho phép microphone nếu test giọng nói.
- Không mở cùng một phiên trên nhiều tab trong khi test xác nhận hoặc hành trình.
- Bắt đầu ở trạng thái đỗ `P`.
- Nếu một ca làm thay đổi cửa, kính, nhiệt độ hoặc bộ nhớ, thực hiện bước dọn dữ liệu được ghi ở cuối nhóm.
- Các số tài nguyên, thời gian xử lý, pin và tầm hoạt động có thể khác theo máy; kiểm tra tính hợp lý, không so với một con số cố định.

## 4. Hướng dẫn nhanh cho tester mới

Khi mở app, chọn một trong hai vai trò:

- **Tài xế**: giao diện trợ lý ViVi và xe mô phỏng.
- **Kỹ sư hệ thống**: giám sát runtime, Digital Twin và các mô phỏng kỹ thuật.

Trong màn hình Tài xế:

- Ô **Nhập điều bạn cần…** dùng để nhập câu lệnh; nhấn nút gửi hoặc `Enter`.
- Nút sóng/microphone dùng để nói; nhấn lần nữa để dừng nếu ViVi chưa tự dừng.
- Thanh nút phía dưới lần lượt có Điều hoà, Cửa sổ, Cửa/capo/cốp, Âm nhạc, Hành trình, Trạng thái xe, Cẩm nang, Loa, số `P/D`, Monitor và Cấu hình.
- Lệnh mở cửa hoặc cửa sổ nhập bằng hội thoại phải được xác nhận ở lượt tiếp theo. Nhập **Xác nhận** để đồng ý hoặc **Hủy** để từ chối.
- Chạm trực tiếp vào một control cụ thể trên bảng thân xe được xem là đồng ý cho đúng thao tác đó.
- `P` là đỗ xe; `D` là đang lái trong mô phỏng. Một số thao tác nguy hiểm phải bị chặn ở `D`.

## 5. Luồng 1 — Mở app và chọn vai trò

### UAT-001 — Trang chọn vai trò

**Bước thực hiện**

1. Mở URL test.
2. Quan sát trang đầu tiên.

**Mong đợi**

- Có tiêu đề **Chọn không gian của bạn**.
- Có hai lựa chọn **Tài xế** và **Kỹ sư hệ thống**.
- Không có màn hình trắng, lỗi JavaScript hoặc vòng tải vô hạn.

### UAT-002 — Vào không gian Tài xế

1. Chọn **Tài xế**.
2. Chờ giao diện tải xong.

**Mong đợi:** giao diện ViVi xuất hiện, trạng thái ban đầu là **ViVi đang ở đây**, có ô nhập và dock điều khiển. Không hiển thị dashboard kỹ sư.

### UAT-003 — Vai trò được bảo vệ

1. Khi đang ở vai trò Tài xế, nhập trực tiếp `/engineer` vào URL.

**Mong đợi:** không được xem dữ liệu kỹ sư; app chuyển về trang chọn vai trò hoặc từ chối truy cập.

### UAT-004 — Chọn vai trò Kỹ sư

1. Quay về `/` trong phiên mới hoặc xóa phiên hiện tại.
2. Chọn **Kỹ sư hệ thống**.

**Mong đợi:** mở **Engineer Dashboard**, có nút **Đổi vai trò** và các khu vực System Overview, Fleet / Digital Twins, Scenario Lab, Model Profiles, OTA Simulation, Pipeline & Eval Metrics và Audit.

## 6. Luồng 2 — Hội thoại văn bản cơ bản

Quay lại vai trò **Tài xế** trước khi chạy nhóm này.

### UAT-005 — Gửi câu lệnh bằng nút gửi

1. Nhập `Trạng thái xe`.
2. Nhấn nút mũi tên gửi.

**Mong đợi**

- ViVi lần lượt hiển thị trạng thái đang xử lý rồi trả lời.
- Câu trả lời dùng từ thân thiện, có pin/tầm hoạt động hoặc trạng thái cabin; không đọc các mã nội bộ như `power_state`, `door_states`, `front_passenger` cho người dùng.
- Ô nhập được mở lại sau khi lượt hoàn tất.

### UAT-006 — Gửi bằng phím Enter

1. Nhập `Đặt nhiệt độ 25 độ`.
2. Nhấn `Enter`.

**Mong đợi:** lệnh được gửi đúng một lần; nhiệt độ cabin trở thành `25°C` và giao diện báo hoàn tất.

### UAT-007 — Không nhận lệnh trống

1. Để trống ô nhập hoặc chỉ nhập khoảng trắng.
2. Nhấn nút gửi.

**Mong đợi:** không tạo lượt mới, không thay đổi trạng thái xe và không xuất hiện lỗi.

### UAT-008 — Không bỏ qua lệnh khi hệ thống bận

1. Gửi một câu hỏi cẩm nang có thời gian xử lý, ví dụ `Hướng dẫn sạc pin VF8 như thế nào?`.
2. Ngay khi ViVi đang xử lý, thử bấm vào ô nhập và gửi thêm lệnh.

**Mong đợi:** ô nhập/nút gửi bị khóa hoặc UI phản hồi rõ rằng ViVi đang xử lý lượt trước. Không được im lặng nuốt câu lệnh.

### UAT-009 — Lỗi kết nối có thể hiểu được

Ca này cần điều phối viên tạm dừng backend sau khi trang đã mở.

1. Nhập `Trạng thái xe`.

**Mong đợi:** UI nói rõ không kết nối được pipeline/backend; không báo xe đã thực hiện lệnh và không tự giả lập thành công.

## 7. Luồng 3 — Điều khiển cabin và multimedia

### UAT-010 — Điều hoà bằng hội thoại

1. Gửi `Đặt nhiệt độ 24 độ`.
2. Mở **Trạng thái xe**.
3. Gửi `Giảm nhiệt độ 2 độ` và mở lại **Trạng thái xe**.

**Mong đợi:** nhiệt độ lần lượt là `24°C` rồi `22°C`; cả lời đáp và Digital Twin đồng nhất.

### UAT-011 — Giới hạn nhiệt độ

1. Gửi `Đặt nhiệt độ 35 độ`.

**Mong đợi:** ViVi từ chối hoặc yêu cầu giá trị trong giới hạn; nhiệt độ xe không thành `35°C`.

### UAT-012 — Nút tăng/giảm điều hoà

1. Bấm biểu tượng **Điều hoà**.
2. Bấm `+` một lần rồi `−` một lần.

**Mong đợi:** mỗi lần bấm chỉ thay đổi 1°C; giá trị cuối trở về ban đầu; popup có thể đóng/mở bình thường.

### UAT-013 — Phát và dừng nhạc

1. Gửi `Phát nhạc thư giãn`.
2. Mở **Trạng thái xe** và kiểm tra Multimedia.
3. Gửi `Dừng nhạc` và kiểm tra lại.

**Mong đợi:** trạng thái đổi từ **Đang phát (mô phỏng)** sang **Đang dừng**; không tuyên bố đã phát âm thanh thật nếu adapter chỉ mô phỏng.

### UAT-014 — Nút Âm nhạc

1. Bấm biểu tượng nốt nhạc hai lần, chờ mỗi lượt hoàn tất.

**Mong đợi:** lần đầu phát, lần sau dừng; trạng thái `aria-pressed`/hình thức active thay đổi tương ứng.

### UAT-015 — Sưởi ghế theo vị trí

1. Gửi `Sưởi ghế sau trái mức 2`.
2. Mở **Trạng thái xe**.
3. Gửi `Tắt sưởi ghế sau trái`.

**Mong đợi:** mục **Sau trái** hiển thị mức 2 rồi mức 0. Các ghế khác không thay đổi. Thao tác này không yêu cầu xác nhận cửa/kính.

### UAT-016 — Thiếu vị trí cabin

1. Gửi `Mở cửa sổ`.

**Mong đợi:** ViVi hỏi vị trí cụ thể, không tự chọn cửa sổ và không làm thay đổi xe.

## 8. Luồng 4 — Xác nhận và policy an toàn

Đảm bảo xe đang ở `P`, tất cả cửa và cửa sổ đang đóng.

### UAT-017 — Xác nhận mở cửa sổ

1. Gửi `Mở cửa sổ bên tài 50%`.
2. Trước khi xác nhận, mở **Trạng thái xe** hoặc popup **Cửa sổ**.
3. Gửi `Xác nhận`.

**Mong đợi**

- Sau bước 1, ViVi ở trạng thái chờ xác nhận và cửa sổ vẫn chưa đổi.
- Sau bước 3, đúng cửa sổ bên tài thành 50%; cửa sổ khác không đổi.
- Lượt xác nhận trên Monitor phải kết thúc ở trạng thái hoàn tất/đã hủy/bị chặn, không treo **đang xử lý**.

### UAT-018 — Hủy thao tác nhạy cảm

1. Gửi `Mở cửa bên phụ`.
2. Gửi `Hủy`.

**Mong đợi:** thao tác bị hủy, cửa bên phụ vẫn đóng; ViVi phản hồi rõ đã hủy.

### UAT-019 — Xác nhận hết hạn

1. Gửi `Mở cửa sổ sau trái`.
2. Chờ hơn 30 giây.
3. Gửi `Xác nhận`.

**Mong đợi:** xác nhận bị từ chối vì hết hạn; cửa sổ vẫn đóng.

### UAT-020 — Xác nhận chỉ dùng một lần

1. Gửi một lệnh mở cửa sổ và xác nhận thành công.
2. Gửi `Xác nhận` lần nữa mà không tạo yêu cầu mới.

**Mong đợi:** không thực hiện thêm thao tác; app không dùng lại quyền xác nhận cũ.

### UAT-021 — Chặn mở cửa/kính khi đang lái

1. Nhấn nút `P` để chuyển sang `D`.
2. Gửi `Mở cửa sổ bên tài` rồi thử `Mở cửa tài`.

**Mong đợi:** cả hai thao tác nguy hiểm bị policy chặn, không tạo thay đổi dù người dùng nói xác nhận. Lời đáp giải thích xe đang chạy/không an toàn.

3. Nhấn `D` để trở lại `P` trước khi tiếp tục.

### UAT-022 — Điều khiển thân xe trực tiếp

1. Mở bảng **Cửa, capo và cốp**.
2. Chạm **Cốp**, chờ kết quả, rồi chạm lại để đóng.
3. Mở bảng **Cửa sổ**, chạm **Bên phụ**, chờ kết quả, rồi chạm lại để đóng.

**Mong đợi:** mỗi lần chạm cụ thể được xem là đồng ý đúng thao tác đó; không xuất hiện bước xác nhận hội thoại dư thừa. Nhãn **Đang mở/Đã đóng** và phần Trạng thái xe đồng bộ.

### UAT-023 — Thao tác hàng loạt

1. Ở `P`, thử **Mở tất cả** trong bảng cửa sổ.
2. Sau khi hoàn tất, chọn **Đóng tất cả**.
3. Thử **Khoá tất cả cửa** trong bảng thân xe.

**Mong đợi:** cả bốn vị trí thay đổi đúng; summary không mâu thuẫn với từng vị trí; không còn cửa sổ mở sau bước đóng tất cả.

## 9. Luồng 5 — Trạng thái xe, cảnh báo và cẩm nang

### UAT-024 — Trạng thái xe có nhãn dành cho người dùng

1. Mở **Trạng thái xe**.

**Mong đợi:** thấy Pin/tầm hoạt động, nhiệt độ cabin, nhiệt độ hệ truyền động, chế độ nguồn, capo, cốp, bốn vị trí cabin, áp suất bốn lốp và Multimedia. Tên vị trí là **Bên tài**, **Bên phụ**, **Sau trái**, **Sau phải**, không phải mã lập trình.

### UAT-025 — Câu hỏi cẩm nang có nguồn

1. Gửi `Đèn cảnh báo áp suất lốp có nghĩa là gì?`.

**Mong đợi:** app tự mở **Cẩm nang xe**; câu trả lời liên quan áp suất lốp; có ít nhất một mục **Nguồn** hoặc section/source ID. Không trộn số đo hiện tại của xe vào nội dung cẩm nang nếu không được hỏi.

### UAT-026 — Cẩm nang không đủ bằng chứng

1. Hỏi một thông tin không có trong cẩm nang, ví dụ `Mật khẩu Wi-Fi nhà tôi là gì?`.

**Mong đợi:** ViVi nói không có đủ thông tin/không tìm thấy nguồn phù hợp; không bịa mật khẩu hoặc đưa ra hướng dẫn không liên quan.

### UAT-027 — Phân biệt trạng thái thật và kiến thức cẩm nang

1. Gửi `Pin hiện tại còn bao nhiêu?`.
2. Gửi `Pin thấp thì xe cảnh báo như thế nào?`.

**Mong đợi:** câu 1 đọc Digital Twin hiện tại; câu 2 tra kiến thức/cẩm nang. Hai câu không bị định tuyến nhầm.

## 10. Luồng 6 — Bộ nhớ người dùng

Nhóm này có ghi dữ liệu lâu dài. Cuối nhóm phải chạy UAT-034 để dọn dữ liệu.

### UAT-028 — Nhớ tên

1. Gửi `Từ nay gọi tôi là Nam`.
2. Gửi `Tên tôi là gì?`.

**Mong đợi:** ViVi xác nhận đã nhớ và trả lời tên **Nam**.

### UAT-029 — Nhớ và áp dụng nhiệt độ yêu thích

1. Gửi `Ghi nhớ nhiệt độ tôi thích là 24 độ`.
2. Xác nhận nhiệt độ xe chưa tự đổi chỉ vì ghi nhớ.
3. Gửi `Đặt điều hoà theo sở thích của tôi`.

**Mong đợi:** bước 1 chỉ lưu sở thích; bước 3 mới đặt cabin về 24°C.

### UAT-030 — Nhớ sở thích nhạc

1. Gửi `Ghi nhớ tôi thích nhạc jazz`.
2. Gửi `Tôi thích nhạc gì?`.
3. Gửi `Phát nhạc tôi thích`.

**Mong đợi:** nhớ đúng **jazz**; lệnh cuối tạo thao tác phát nhạc jazz hoặc phản hồi theo khả năng của media adapter.

### UAT-031 — Ghi chú có tên

1. Gửi `Ghi nhớ ghi chú địa chỉ nhà: nhà tôi ở Huế`.
2. Gửi `Bạn nhớ gì về địa chỉ nhà?`.

**Mong đợi:** trả lời đúng nội dung ghi chú, không chuyển sang tra cẩm nang.

### UAT-032 — Quên một mục

1. Gửi `Quên ghi chú địa chỉ nhà`.
2. Hỏi lại `Bạn nhớ gì về địa chỉ nhà?`.

**Mong đợi:** ViVi không còn trả lại nội dung đã quên và không học lại mục đó từ lịch sử hội thoại.

### UAT-033 — Bộ nhớ còn sau khi tải lại

1. Tải lại trang hoặc đóng/mở lại trình duyệt.
2. Vào vai trò Tài xế và hỏi `Tên tôi là gì?`.

**Mong đợi:** vẫn nhớ **Nam** nếu dùng cùng profile và database.

### UAT-034 — Xóa toàn bộ bộ nhớ

1. Gửi `Xoá toàn bộ bộ nhớ của tôi`.
2. Nếu ViVi yêu cầu xác nhận, trả lời `Xác nhận`.
3. Hỏi `Bạn nhớ gì về tôi?`.

**Mong đợi:** các mục tên, sở thích và ghi chú vừa tạo không còn. ViVi không tự khôi phục chúng từ lịch sử.

## 11. Luồng 7 — Microphone, STT, TTS và cấu hình

Nếu STT/TTS được cấu hình `off`, đánh dấu các ca phụ thuộc tương ứng là `N/A` hoặc `BLOCKED`, không đánh `FAIL`.

### UAT-035 — Push-to-talk và transcript

1. Cho phép quyền microphone.
2. Nhấn nút sóng, nói rõ `Đặt nhiệt độ 23 độ`.
3. Im lặng để ViVi tự dừng hoặc nhấn nút lần nữa.

**Mong đợi:** trạng thái đi qua nghe → nhận diện → xử lý; transcript nhìn thấy được và đúng ý; nhiệt độ thành 23°C. Nếu transcript sai, app không được che giấu nội dung đã nhận.

### UAT-036 — Từ chối quyền microphone

1. Chặn quyền microphone trong trình duyệt.
2. Nhấn nút sóng.

**Mong đợi:** có thông báo dễ hiểu; app không treo và ô nhập văn bản vẫn dùng được.

### UAT-037 — Bật/tắt loa

1. Khi loa đang bật, gửi một câu hỏi ngắn và xác nhận có phản hồi âm thanh nếu TTS khả dụng.
2. Bấm biểu tượng **Loa** để tắt.
3. Gửi câu hỏi khác.

**Mong đợi:** khi tắt loa, âm thanh đang phát dừng và lượt sau chỉ hiện chữ. Bật lại loa thì phản hồi âm thanh hoạt động trở lại.

### UAT-038 — Cấu hình model và giọng

1. Mở **Cấu hình ViVi**.
2. Quan sát **Mô hình hội thoại**, **Giọng phản hồi** và **Nhận diện giọng nói**.
3. Chọn một provider được đánh dấu sẵn sàng; thử bấm một provider bị vô hiệu nếu có.

**Mong đợi:** chỉ provider khả dụng mới chọn được; lựa chọn áp dụng ở lượt kế tiếp; UI không yêu cầu hoặc hiển thị API key. Provider không sẵn sàng có lý do rõ ràng.

### UAT-039 — Giảm chuyển động

1. Trong Cấu hình, bật **Giảm chuyển động**.
2. Đóng panel và quan sát lõi/hiệu ứng.
3. Tải lại trang.

**Mong đợi:** chuyển động trang giảm rõ rệt và lựa chọn được giữ sau tải lại. Tắt tùy chọn để hoàn nguyên.

### UAT-040 — Wake word (nếu được hỗ trợ)

1. Bật **Gọi “ViVi ơi”**.
2. Nói `ViVi ơi`, chờ trạng thái nghe rồi nói một câu lệnh.

**Mong đợi:** audio chỉ được gửi sau khi phát hiện từ khóa; không mở nhiều lượt nghe chồng nhau. Nếu trình duyệt/thiết bị không hỗ trợ, UI phải báo trạng thái thay vì giả vờ hoạt động.

## 12. Luồng 8 — Hành trình và VietMap

Nhóm này cần VietMap và quyền vị trí/mạng phù hợp. Nếu môi trường không có API key hoặc không thể truy cập VietMap, ghi `BLOCKED` cùng nguyên nhân.

### UAT-041 — Mở/đóng Hành trình

1. Bấm **Hành trình**.
2. Quan sát panel và bản đồ.
3. Bấm `×` để đóng rồi mở lại.

**Mong đợi:** có tiêu đề **Hôm nay, mình đi đâu?**, xuất phát, ô điểm đến và bản đồ. Đóng/mở không làm mất một kế hoạch đã có và không tự khởi hành.

### UAT-042 — Quyền vị trí và lấy lại vị trí

1. Cho phép vị trí khi trình duyệt hỏi.
2. Bấm nút lấy lại vị trí hiện tại.

**Mong đợi:** vị trí xuất phát được cập nhật hoặc có thông báo fallback rõ ràng. Từ chối quyền vị trí không được làm app treo.

### UAT-043 — Tìm và chọn điểm đến

1. Trong ô **Tìm địa điểm, địa chỉ…**, nhập `Tràng An, Ninh Bình`.
2. Chờ danh sách kết quả.
3. Chọn một kết quả phù hợp.

**Mong đợi:** danh sách có tên/địa chỉ để phân biệt; chọn kết quả tạo tuyến xem trước với thời gian, quãng đường, giờ đến và các bước chỉ đường. Không tự chuyển sang `D`.

### UAT-044 — Tìm địa điểm bằng hội thoại và làm rõ

1. Gửi `Đi Ninh Bình`.
2. Trả lời các câu làm rõ bằng `Không, cứ lấy trung tâm` hoặc cung cấp `Tràng An` khi được hỏi.

**Mong đợi:** mỗi lượt chỉ hỏi một câu; ViVi không hiểu câu từ chối đổi điểm thành đồng ý khởi hành; tuyến chỉ được chuẩn bị sau khi địa điểm đủ rõ.

### UAT-045 — Bắt đầu, tốc độ và tạm dừng hành trình

1. Với một tuyến xem trước, bấm **Bắt đầu hành trình** hoặc xác nhận bằng hội thoại.
2. Thử các tốc độ `1×`, `5×`, `15×`.
3. Nhấn `P`, sau đó `D` để tiếp tục.

**Mong đợi:** hành trình chỉ chạy sau xác nhận; marker/tiến trình di chuyển theo tuyến; đổi tốc độ không nhảy lùi; `P` dừng chuyển động nhanh, `D` tiếp tục hành trình đã tạm dừng.

### UAT-046 — Điều khiển bản đồ

1. Kéo/zoom bản đồ, sau đó bấm **Về vị trí xe**.
2. Bấm **Ẩn bản đồ & kế hoạch**, rồi mở lại.
3. Bấm **Chi tiết/Thu gọn** nếu xuất hiện.

**Mong đợi:** recenter đúng vào xe; ẩn/mở không dừng mô phỏng; nội dung không che mất nút đóng, mic hoặc dock ở kích thước màn hình đang test.

### UAT-047 — Kết thúc hành trình

1. Bấm **Kết thúc hành trình**.
2. Mở lại Hành trình.

**Mong đợi:** chuyến đang chạy kết thúc, tiến trình không tự tiếp tục; có thể tạo hành trình mới mà không dùng nhầm xác nhận/tuyến cũ.

### UAT-048 — Mất mạng/map lỗi

Ca này cần điều phối viên mô phỏng mất mạng hoặc chặn VietMap.

1. Thử tìm địa điểm và mở lại panel.

**Mong đợi:** có thông báo lỗi/retry; ô tìm kiếm và các chức năng ViVi không phụ thuộc map vẫn dùng được; không có màn hình trắng.

## 13. Luồng 9 — Edge Monitor

Mở Monitor bằng biểu tượng đồng hồ đo trên dock hoặc URL `/monitor`.

### UAT-049 — Kết nối và tài nguyên

1. Chờ Monitor cập nhật hai chu kỳ.

**Mong đợi:** trạng thái chuyển từ **Đang kết nối** sang online; RAM, CPU load, Disk có số/chi tiết hợp lý. GPU có thể báo chưa đo/không khả dụng trên máy không có GPU. Trên macOS, RAM không được luôn là `N/A` và RSS không được có giá trị phi thực tế cỡ terabyte.

### UAT-050 — Trạng thái pipeline

1. Kiểm tra khu vực **Trạng thái pipeline**.

**Mong đợi:** STT, LLM, TTS, RAG và vehicle có provider/trạng thái rõ ràng. Thành phần tắt theo cấu hình được ghi là tắt/chưa sẵn sàng, không giả là healthy.

### UAT-051 — Biểu đồ tài nguyên

1. Chuyển lần lượt RAM, CPU load, GPU peak 20s, GPU memory và Disk.

**Mong đợi:** nút được chọn thay đổi đúng; biểu đồ tiếp tục cập nhật; metric không khả dụng được thể hiện rõ, không làm trang lỗi.

### UAT-052 — Lượt gần đây và chi tiết lượt

1. Quay lại Không gian, gửi `Trạng thái xe` và một lệnh cần xác nhận.
2. Mở lại Monitor.
3. Chọn từng lượt trong **Các lượt gần đây**.

**Mong đợi:** lượt mới xuất hiện; **Chi tiết lượt** hiển thị timing/stage có dữ liệu tương ứng. Lượt xác nhận đã xử lý có trạng thái kết thúc, không ở **đang xử lý** mãi.

### UAT-053 — Reset lịch sử hội thoại

1. Nhấn **Reset lịch sử**.
2. Xác nhận thao tác nếu được hỏi.
3. Quay lại Không gian và hỏi về nội dung hội thoại trước đó.

**Mong đợi:** lịch sử hội thoại của profile hiện tại được xóa/reset. Long-term memory không bị xóa bởi nút này; xác nhận cũ không còn hiệu lực. Nếu retention transcript trên trình duyệt được bật, kiểm tra phần lịch sử trình duyệt theo cấu hình của môi trường.

### UAT-054 — Backend ngắt khi đang mở Monitor

1. Nhờ điều phối viên dừng backend.
2. Chờ một chu kỳ refresh.

**Mong đợi:** Monitor hiển thị offline/mất kết nối, giữ giao diện ổn định và phục hồi sau khi backend chạy lại.

## 14. Luồng 10 — Engineer Dashboard

Chọn vai trò **Kỹ sư hệ thống**. Mọi Scenario, model profile và OTA trên trang đều là mô phỏng/trạng thái runtime local; không được mô tả như đã triển khai lên xe thật.

### UAT-055 — System Overview

1. Chờ trang tải xong, sau đó bấm **Làm mới**.

**Mong đợi:** có card STT, LLM, TTS, RAG, Vehicle, Uptime, Device và Pipeline gần nhất. Nút Làm mới cập nhật thời gian/thông báo đồng bộ, không nhân đôi card.

### UAT-056 — Fleet / Digital Twins

1. Kiểm tra xe runtime và các twin `demo-car-2`, `demo-car-3`.

**Mong đợi:** mỗi xe có nguồn (`memory runtime`, `mqtt live` hoặc `simulated twin`), trạng thái online/availability, pin/range, nhiệt độ, nguồn/cửa, software profile, lốp và cảnh báo. Twin mô phỏng được ghi nhãn rõ.

### UAT-057 — Scenario Lab: cảnh báo và reset

1. Chọn `demo-car-2`.
2. Áp dụng lần lượt **Pin thấp 15%**, **Lốp trước trái thấp 205 kPa**, **Hệ truyền động 95°C**.
3. Sau mỗi lần, kiểm tra card xe và cảnh báo.
4. Áp dụng **Reset mặc định**.

**Mong đợi:** đúng giá trị/cảnh báo thay đổi; xe khác không bị ảnh hưởng; reset đưa twin về mặc định và audit ghi lại từng thao tác.

### UAT-058 — Scenario mất kết nối

1. Chọn một Digital Twin mô phỏng.
2. Áp dụng **Mất kết nối twin**.
3. Chọn xe runtime thật/memory và thử cùng scenario nếu UI cho phép.

**Mong đợi:** twin thành unavailable/offline; scenario này bị từ chối cho runtime vehicle và có thông báo dễ hiểu. Dùng **Reset mặc định** để kết nối lại twin.

### UAT-059 — Model Profiles

1. Chọn **PC offline** rồi **Jetson/Nano offline**.
2. Cuối cùng chọn **Runtime hiện tại**.

**Mong đợi:** nhãn **Đang chọn** chuyển đúng profile; audit ghi `model.select`. Việc chọn profile mô phỏng không được thay đổi provider thật trong `config.toml` hoặc tuyên bố đã nạp model.

### UAT-060 — OTA thành công và rollback

1. Nhấn **Mô phỏng thành công**.
2. Nhấn **Mô phỏng rollback**.

**Mong đợi:** lịch sử có hai chuỗi trạng thái; lượt thành công kết thúc `COMPLETED`, lượt rollback kết thúc `ROLLED_BACK`; đều có nhãn simulation và xuất hiện trong Audit.

### UAT-061 — Audit

1. Sau khi chạy scenario, chọn model và OTA, kiểm tra **Audit**.

**Mong đợi:** mỗi sự kiện có action, target, thời gian, actor và detail; thứ tự mới nhất ở trên; refresh không làm mất sự kiện trong cùng phiên backend.

### UAT-062 — Đổi vai trò

1. Nhấn **Đổi vai trò**.

**Mong đợi:** phiên Engineer kết thúc và quay về trang chọn vai trò. URL `/engineer` không còn truy cập được nếu chưa chọn lại vai trò.

## 15. Kiểm tra giao diện và khả năng sử dụng

Chạy ít nhất trên desktop `1440×900` và mobile `390×844`.

### UAT-063 — Responsive và cuộn

1. Thay đổi kích thước trình duyệt giữa desktop và mobile.
2. Mở lần lượt Cấu hình, Trạng thái xe, Cẩm nang, Hành trình và các popup cabin.

**Mong đợi:** không có nội dung quan trọng tràn khỏi màn hình; panel cuộn được; nút đóng, ô nhập, mic và dock vẫn thao tác được; không xuất hiện thanh cuộn ngang bất thường.

### UAT-064 — Bàn phím và focus

1. Dùng `Tab`, `Shift+Tab`, `Enter` và `Space` để đi qua các nút chính.
2. Mở/đóng dialog bằng bàn phím.

**Mong đợi:** focus nhìn thấy được, thứ tự hợp lý, nút có tên dễ hiểu cho screen reader; focus không bị kẹt sau khi dialog đóng.

### UAT-065 — Nội dung lỗi không chỉ dùng màu

1. Quan sát các trạng thái blocked/offline/unavailable.

**Mong đợi:** luôn có chữ hoặc biểu tượng/nhãn giải thích, không bắt người dùng chỉ phân biệt bằng màu.

## 16. Smoke test rút gọn trước khi bàn giao

Nếu không đủ thời gian chạy toàn bộ, tối thiểu chạy các ca sau theo thứ tự:

`UAT-001`, `UAT-002`, `UAT-005`, `UAT-006`, `UAT-010`, `UAT-017`, `UAT-018`, `UAT-021`, `UAT-024`, `UAT-025`, `UAT-041`, `UAT-043`, `UAT-045`, `UAT-049`, `UAT-052`, `UAT-055`, `UAT-057`, `UAT-060`, `UAT-063`.

Không ký duyệt nếu có `P0/P1`, ca safety/xác nhận bị `FAIL`, hoặc Monitor báo thành công khác với trạng thái xe thực tế.

## 17. Mẫu ghi nhận lỗi

Sao chép mẫu này cho mỗi lỗi:

```text
Tiêu đề: [P?][UAT-xxx] Mô tả ngắn

Môi trường:
- Commit/phiên bản:
- URL:
- OS/trình duyệt:
- STT / LLM / TTS / Vehicle provider:

Điều kiện trước:

Các bước tái hiện:
1.
2.
3.

Kết quả thực tế:

Kết quả mong đợi:

Tần suất: x/y lần
Thời điểm xảy ra:
Ảnh/video/log đính kèm:
Có khắc phục tạm thời không:
```

Khi lỗi liên quan một lượt hội thoại, ghi thêm câu người dùng đã nhập/nói, transcript ViVi nhận được, trạng thái cuối và thời điểm để đối chiếu trong `/monitor`.

## 18. Bảng tổng kết nghiệm thu

| Nhóm | Tổng ca | PASS | FAIL | BLOCKED | N/A |
| --- | ---: | ---: | ---: | ---: | ---: |
| Vai trò | 4 |  |  |  |  |
| Hội thoại văn bản | 5 |  |  |  |  |
| Cabin/multimedia | 7 |  |  |  |  |
| Xác nhận/safety | 7 |  |  |  |  |
| Trạng thái/cẩm nang | 4 |  |  |  |  |
| Bộ nhớ | 7 |  |  |  |  |
| Voice/cấu hình | 6 |  |  |  |  |
| Hành trình | 8 |  |  |  |  |
| Monitor | 6 |  |  |  |  |
| Engineer | 8 |  |  |  |  |
| Giao diện/accessibility | 3 |  |  |  |  |
| **Tổng** | **65** |  |  |  |  |

Kết luận: `ĐẠT / ĐẠT CÓ ĐIỀU KIỆN / KHÔNG ĐẠT`

Người test: ____________________  Ngày: __________

Người xác nhận: _______________  Ngày: __________
