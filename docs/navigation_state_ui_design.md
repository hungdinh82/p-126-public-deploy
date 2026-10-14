# ViVi: tính năng chỉ đường, trạng thái và giao diện

Ngày rà soát: 2026-10-10. Đối chiếu code trong workspace và log SQLite đến lượt 1034.

Đây là thiết kế đề xuất để thống nhất lần refactor tiếp theo, chưa phải mô tả runtime đã triển khai. Các thay đổi đang có trong workspace được giữ nguyên. Không đánh giá chất lượng giao diện từ các test mock; cần kiểm tra trực tiếp trình duyệt khi thực hiện thiết kế này.

## 1. Những tính năng đã có

| Nhóm | Chức năng đang có | Phần cần thống nhất |
|---|---|---|
| Điểm xuất phát | GPS trình duyệt, điểm mô phỏng, chọn điểm trên bản đồ, vị trí trên hành trình | Ai sở hữu vị trí hiện tại; xử lý thiếu vị trí; không lấy lại điểm xuất phát cũ |
| Tìm địa điểm | Tên riêng, khu vực, hiệu thuốc/trạm sạc, gần nhất, tối đa ba ứng viên | Phân biệt tìm kiếm độc lập với chọn điểm cho một kế hoạch |
| Phân giải địa điểm | Lựa chọn bằng model, handle, chọn số thứ tự/giọng nói/bấm thẻ; trung tâm vùng đã xác minh; sửa địa điểm | Câu hỏi có kiểu; chọn địa điểm không cấp quyền bắt đầu |
| Lập tuyến dẫn đường | Route VietMap, một chiều/khứ hồi, kiểm tra pin, xem trước | Tuyến nháp phải độc lập với tuyến đang chạy |
| Lập kế hoạch sạc | `trip.plan`, các phương án sạc, trạm thật qua VietMap, thời gian/pin, xem bản đồ, chỉnh và bắt đầu | Là một kết quả tính toán, không phải một hành trình đang chạy |
| Chạy hành trình | Xác nhận, cảnh lái 3D, bản đồ nhỏ, hướng rẽ, ETA, tốc độ mô phỏng | Một nguồn trạng thái; chuyển tuyến phải cập nhật đồng thời cảnh 3D và bản đồ |
| Vòng đời hành trình | Tạm dừng P, tiếp tục D, hoàn tất, huỷ bản nháp, clear trong phiên | Thiếu nghĩa riêng cho kết thúc, xoá kế hoạch, xoá lịch sử |
| Sạc/cảnh báo | Ước tính SOC, dừng sạc trong mô phỏng, cảnh báo pin, đề xuất đổi tuyến | Tách xe đứng yên để sạc khỏi tạm dừng hành trình; cảnh báo không tự đổi tuyến |
| Cách xem | Home, map toàn màn hình, mini map, thẻ địa điểm/kế hoạch, ẩn/mở lại | Nhiều nút lặp; màn hình hiện tại đang ảnh hưởng thao tác được chạy |
| Ghi nhận | Checkpoint, ledger, history lượt hội thoại, ID/revision/event | Chưa có thư viện lịch sử hành trình cho người dùng; nút clear hiện tại không xoá history SQLite |

Các capability hiện tại nằm trong `src/vivi/agents/tools.py`: `navigation.search/plan/select/refine/start/cancel/stop` và `trip.plan/select/refine/start/decline/cancel`. Typed event còn có position, point, select_plan, progress, charge, pause, resume, clear. Có API bản đồ đọc dữ liệu và một endpoint demo đổi trạng thái lái trực tiếp.

## 2. Những điểm gây lỗi hiện tại

### Bằng chứng từ log

| Lượt | Hiện tượng | Kết luận có thể rút ra |
|---|---|---|
| 1026–1027 | Hỏi “Phường Bãi Cháy. Đúng chỗ này chứ?”; người dùng nói “đúng”; trả “Bước này còn thiếu thông tin, chưa thể xác nhận” | Model xuất `act=confirm` của task thay vì chấp nhận ứng viên. Câu hỏi địa điểm chưa được ràng buộc với thao tác trả lời tương ứng |
| 1029–1031 | Tuyến bắt đầu; hai lượt xác nhận tiếp theo bị từ chối | Xác nhận cũ tiếp tục được gửi. Log không chứng minh nguyên nhân là bấm lặp hay render; cần vô hiệu nút đang gửi và loại grant đã dùng |
| 1032–1034 | Pause trả vehicle `driving=false`; hai lượt clear tiếp theo vẫn báo “Chuyển P trước khi xoá hành trình”, navigation vẫn paused | Xe và checkpoint không còn được đối chiếu nhất quán giữa các thao tác. Chưa đủ log để quy chính xác lần ghi nào đã đổi lại vehicle |
| 1019–1020 | Tìm Vinhomes nhưng navigation vẫn chứa Hồ Tây, tuyến mới ở `pending` | Giữ tuyến cũ trong lúc lập tuyến mới là đúng; UI phải trình bày hai vai trò và chỉ thay khi chấp nhận. Một trường selected chung dễ hiển thị sai |
| 1024–1034 | Kế hoạch Đà Nẵng đã bắt đầu vẫn lưu cùng các tuyến mới | Kế hoạch tính toán và hành trình hiện tại phải có danh tính riêng; không dùng kế hoạch cũ làm hành động mặc định |

### Bằng chứng từ code

- `NavigationState` vừa có idle/selecting/preview, vừa có running/charging/paused/completed, đồng thời chứa một bản navigation khác trong `pending`. Tạo bản nháp vì thế phải sao chép và dọn nhiều trường của hành trình.
- `TripPlanState` kế thừa toàn bộ `NavigationState`, gồm progress, pause/resume và trạng thái sạc. Một kế hoạch lại có cả `status`, `calculation_status`, `offer.status`, `started_revision`.
- `journey.js` giữ `backendNavigation`, `backendTripPlan`, `mapTripPlan`, `choiceOwner`, `sim.running`, `view`, `parkRequested` và nhiều cờ ẩn. `#journey-start` đổi tác động theo `mapTripPlan` và `sim.running`.
- `app.js` đổi P/D theo hai đường: event workflow hoặc PUT `/demo/vehicle/driving`. `updateDriveUI` còn gọi pause, trong khi callback `setDriving` có thể ghi ngược trạng thái xe. Render và chạy simulation đang có quyền gây side effect.
- Nút huỷ/ẩn xuất hiện tại toolbar, planning-controls, thẻ địa điểm và thẻ kế hoạch. Một nút “Xoá hành trình đã lưu” thực tế xoá cả tuyến/kế hoạch trong phiên.

Hướng sửa: giữ dịch vụ VietMap, energy và cảnh 3D; thay mô hình trạng thái và cách UI phát thao tác. Chỉ bổ sung prompt hoặc thêm if/else theo từng nút sẽ tiếp tục tạo tổ hợp trạng thái khó kiểm soát.

## 3. Phân chia trạng thái đề xuất

### A. Xe: trạng thái được quan sát

`vehicle.gear = P | D`, vị trí, SOC, trạng thái cửa và version. Với simulator, server thực hiện chuyển P/D. Khi tích hợp GPS/xe thật, chỉ phản ánh dữ liệu adapter và quyền điều khiển mà adapter hỗ trợ.

UI không tự đặt gear sau khi vẽ hoạt cảnh. Mọi thao tác demo đổi gear đi qua cùng bộ điều phối phiên; endpoint demo cũ cần gọi bộ điều phối này nếu tiếp tục được giữ.

### B. Kế hoạch: một bản nháp đang được xử lý, có thể có kết quả đã tính

`PlanDraft` có ID/revision, `purpose = navigation | charging_itinerary | place_search`, query gốc, vùng, xuất phát, điểm đến, một chiều/khứ hồi, ứng viên, lựa chọn, tuyến/sạc và câu hỏi đang chờ.

| Trạng thái | Ý nghĩa | Có được bắt đầu? |
|---|---|---|
| `needs_origin` | Thiếu điểm xuất phát | Không |
| `resolving_destination` | Đang tìm/đánh giá địa điểm | Không |
| `needs_destination` | Đang hỏi đổi điểm mặc định, chọn ứng viên hoặc bổ sung địa chỉ | Không |
| `calculating` | Đã chấp nhận điểm đến, đang tính tuyến/sạc | Không |
| `ready` | Tuyến và kiểm tra năng lượng đủ để dùng | Có, sau chấp nhận đúng offer |
| `incomplete` | Có một phần kết quả nhưng thiếu tuyến, pin, dữ liệu trạm hoặc phương án khả thi | Không; cho xem và sửa/thử lại |
| `cancelled` | Đã huỷ bản nháp | Không |

Không có `running/paused` trong kế hoạch. Bắt đầu tạo một ActiveJourney tham chiếu snapshot bất biến của kế hoạch. Có thể lưu dấu “đã dùng để bắt đầu hành trình X”; dấu này không làm kế hoạch trở thành tuyến đang chạy và không tự tạo lại offer.

`navigation.plan` và `trip.plan` giữ nghĩa sản phẩm khác nhau, dùng chung planner và contract. `navigation.search` hoàn thành khi đưa kết quả tìm kiếm; chọn một kết quả để đi tạo/tiếp tục bản nháp tương ứng. Lập tuyến/kế hoạch hoàn thành khi kết quả đã đủ và được trình bày; đề nghị bắt đầu là offer riêng, không treo các bước nhạc/điều hoà trong cùng yêu cầu. Khi thật sự thiếu slot thì scheduler vẫn dừng ở bước đó.

Một phiên có một bản nháp ở foreground. Yêu cầu đổi đích sửa đúng bản nháp; yêu cầu lập kế hoạch mới thay foreground và vô hiệu lựa chọn cũ. Các tác vụ khác trong hội thoại vẫn do dialogue reducer quản lý, không bị thao tác huỷ tuyến xoá theo.

### C. Hành trình đã được chấp nhận

`ActiveJourney` có journey ID, route revision, snapshot tuyến đã chấp nhận, nguồn plan ID/revision, đích, tiến trình, SOC, thứ tự trạm và chế độ khi tạm dừng.

| Trạng thái | Xe trong simulator | Tiến trình |
|---|---|---|
| Không có | P hoặc D tự do | Không có tuyến đang được bám |
| `moving` | D | Tăng vị trí/quãng đường; cập nhật pin |
| `paused` | P | Giữ vị trí, tuyến, pin và trạm hiện tại; đóng băng tiến trình |
| `charging` | P | Quãng đường đứng yên; chỉ pin/thời gian sạc tăng |
| `completed` | P | Đến đích cuối cùng; khứ hồi là đã quay lại xuất phát |
| `ended` | P | Người dùng kết thúc trước đích; giữ bản ghi kết quả một phần |

Về P khi đang moving tạo paused. Charging đã là P, không vì render P mà chuyển thành paused. Nếu cần nút tạm dừng đồng hồ mô phỏng sạc, đó là điều khiển demo riêng.

Khi vừa tới trạm, chuyển charging và P cùng một cập nhật. Chuyển D khi chưa đạt mức pin rời trạm thì giữ P và giải thích; đạt mức đó thì tiếp tục cùng hành trình. Hoàn thành sạc trong chế độ tự chạy có thể tiếp tục theo kế hoạch đã chấp nhận nếu không có yêu cầu dừng của người dùng. Người dùng chuyển P/tạm dừng rõ ràng phải giữ pause để D mới tiếp tục.

### D. UI: cách xem, không phải quyền thực thi

`screen = home | map`; map có `target = draft:<id/revision> | journey:<id/revision> | result:<id/revision>`. Home tự chọn cảnh xe đỗ/lái theo gear quan sát được.

`presentation = hidden | compact` cho thẻ theo ID, cùng preference bản đồ nhỏ. Mở map/đóng map/ẩn thẻ không thay đổi PlanDraft, ActiveJourney hoặc gear.

Nguồn bản đồ, scene 3D, hướng rẽ và ETA của hành trình đều lấy từ cùng ActiveJourney. Bản nháp dùng lớp preview riêng và nhãn “Tuyến mới”; không dùng selected/route của draft để vẽ cảnh xe chạy.

## 4. Bảng chuyển trạng thái chính

| Sự kiện | Trạng thái trước | Kết quả | Giao diện sau |
|---|---|---|---|
| Tìm địa điểm/lập kế hoạch | Bất kỳ | Tạo draft; giữ ActiveJourney | Home với một thẻ foreground; không tự mở map |
| Chọn ứng viên | Draft needs_destination | Chấp nhận place handle, tính tuyến nếu mục đích cần | Thẻ báo đang tính; vẫn home hoặc map do người dùng đã mở |
| Tính xong | Draft calculating | ready hoặc incomplete | Tóm tắt ngắn; nút phù hợp với khả năng thực hiện |
| Bấm/nói Bắt đầu | Ready, chưa có hành trình | Kiểm tra lại vị trí/pin/policy, chấp nhận đúng offer, tạo journey, simulator chuyển D | Cảnh lái 3D, hướng rẽ, mini map theo preference; đóng map toàn màn hình |
| Chấp nhận thay tuyến | Có journey và draft ready | Tính từ vị trí hiện tại; thay snapshot hành trình sau kiểm tra/xác nhận | 3D, map, ETA cùng chuyển đích. Nếu đang paused thì giữ P/paused, D mới chạy |
| Từ chối bắt đầu | Ready offer | Giữ kết quả; offer declined | Thẻ xem/sửa; xe không đổi |
| Chuyển P | Moving | paused và P | Home xe đỗ; bản đồ nhỏ ẩn; trạng thái tiếp tục chỉ trong workspace nếu người dùng mở |
| Chuyển D | Paused có tuyến đã chấp nhận | Revalidate, tiếp tục vị trí cũ; lỗi thì vẫn P | Cảnh lái 3D; không phát lại start |
| Chuyển D | Không có journey; draft ready hoặc chưa đủ | Chạy demo tự do theo policy, không chấp nhận draft | Cảnh lái; thẻ kế hoạch nếu đang hiện vẫn chờ Bắt đầu |
| Chuyển D | Charging chưa đủ pin rời trạm | Không rời trạm; giải thích | Giữ P và thẻ tiến trình sạc |
| Huỷ bản nháp | Draft bất kỳ | cancelled, vô hiệu offer/handle; tác vụ khác giữ nguyên | Bỏ thẻ draft; ActiveJourney giữ nguyên |
| Kết thúc hành trình | Moving/paused/charging | ended, dừng simulator, về P, ghi kết quả một phần | Home; không tự khôi phục khi bấm D |
| Tới đích cuối | Moving | completed, P, ghi kết quả | Home; tóm tắt ngắn có thể ẩn |
| Ẩn/đóng map | Bất kỳ | Chỉ đổi UI | Trở lại home; hành trình tiếp tục nếu đang moving |
| Cảnh báo thiếu pin | Journey hiện tại | Cảnh báo + draft đề xuất trạm; không thay tuyến | Một thẻ đề xuất có nút chấp nhận/thay tuyến |

Với hành trình A paused và bản nháp B ready: D tiếp tục **A**. Muốn đi B phải bấm “Thay tuyến sang B”/nói rõ chấp nhận B trước. Kế hoạch B đang được xem không được làm thay đổi nghĩa nút D.

## 5. Nút bấm và vị trí

Mỗi nút gửi action có target ID/revision cố định. Nhãn/khả năng bấm được suy từ trạng thái domain; không suy thao tác từ `sim.running` hay màn hình map đang mở.

| Nút | Ở đâu | Tác động duy nhất |
|---|---|---|
| Chọn địa điểm | Trong tối đa ba thẻ ứng viên | Chấp nhận place handle; chưa chạy |
| Giữ trung tâm / Đổi địa điểm | Thẻ điểm mặc định | Trả lời câu hỏi đổi điểm; chưa chạy |
| Bắt đầu hành trình | Kế hoạch ready, chưa có journey đang hoạt động | Chấp nhận và chạy kế hoạch này |
| Thay tuyến sang [đích] | Draft ready, có journey moving/paused/charging | Chấp nhận thay tuyến này; giữ pause nếu P, xử lý điều kiện sạc trước khi di chuyển |
| Xem bản đồ | Thẻ kết quả hoặc mục Chỉ đường trong dock | Mở map đúng đối tượng; không chạy |
| Chỉnh điểm đến/kế hoạch | Thẻ kết quả | Tạo revision mới của draft; không sửa snapshot đang chạy |
| Huỷ yêu cầu | Trong draft hoặc workspace | Chỉ huỷ draft đó; không huỷ các capability khác |
| Tạm dừng · P / Tiếp tục · D | Điều khiển gear chính; map có thể phản ánh cùng action | Cùng một event backend; không có đường PUT fixture riêng |
| Kết thúc hành trình | Menu của ActiveJourney | Kết thúc journey, không phải pause và không phải xoá log |
| Ẩn | Thẻ foreground/mini map | Ẩn UI theo ID; không huỷ |
| Đóng map | Map | Trở về home; không pause |
| Bỏ kế hoạch khỏi phiên | Menu kết quả đang lưu | Bỏ kế hoạch đó; không bỏ snapshot đang chạy |
| Xoá lịch sử hành trình | Danh sách hành trình đã kết thúc | Xoá dữ liệu lịch sử sản phẩm được chọn; không xoá ledger/audit hội thoại |

Loại bỏ toolbar clear tổng hợp và các nút huỷ trùng bên ngoài thẻ. Workspace được truy cập qua mục Chỉ đường đang có trong dock; không cần nút “Kế hoạch” nổi cố định trên home P.

Không dùng một nút chung thay lần lượt các nghĩa Bắt đầu/Tạm dừng/Tiếp tục/Bắt đầu kế hoạch khác. Map có vùng hành động của **đối tượng đang xem**, gear là điều khiển xe riêng.

### Layout

| Bối cảnh | Cảnh nền | Nội dung nổi | Nút chính |
|---|---|---|---|
| P, chưa có yêu cầu | Xe đỗ full màn hình như ban đầu | Không có bản đồ/thẻ kế hoạch | Dock và giọng nói |
| P, đang chọn điểm | Xe đỗ full màn hình | Một panel nhỏ, mờ bên phải; tối đa ba ô; Ẩn/Huỷ trong panel | Chọn điểm hoặc bổ sung địa chỉ |
| P, kế hoạch ready | Xe đỗ full màn hình | Một thẻ quãng đường/thời gian/điểm sạc; có thể ẩn toàn bộ | Bắt đầu, Xem bản đồ; chỉnh/huỷ trong menu |
| P, journey paused | Xe đỗ full màn hình | Mặc định không mini map; xem trạng thái khi mở workspace | Gear D tiếp tục; Kết thúc trong workspace |
| D, journey moving | Cảnh lái 3D hiện có | Hướng rẽ, ETA, mini map; draft mới là một thẻ có nhãn Tuyến mới | Gear P; thao tác thay tuyến có đích rõ |
| Sạc | Xe đứng yên | SOC và thời gian tới mức rời trạm; có thể ẩn thẻ | Tiếp tục khi hợp lệ; kết thúc trong menu |
| Map do người dùng mở | Bản đồ lớn | Header/action cố định, danh sách/hướng rẽ/sạc cuộn riêng | Theo target map; nút Đóng |

Không dành cột trống làm giảm chiều rộng home. Panel foreground có giới hạn chiều cao, nền mờ và vùng cuộn; không che giọng nói, dock hay gear. Màn hình nhỏ dùng panel thấp gọn và vùng an toàn quanh điều khiển. Khi đang lái, không ghép một thẻ kế hoạch cũ và một danh sách địa điểm mới thành nhiều lớp cùng nổi.

Ẩn giữ preference theo ID; progress/stream/re-render không mở lại. Một yêu cầu mới do người dùng bắt đầu có ID mới thì được hiện thẻ. Về P ẩn mini map. Nếu người dùng đã ẩn cả thẻ, home P không có nút nổi mở lại; mở từ dock Chỉ đường. Cảnh báo quan trọng vẫn có vùng trình bày riêng.

## 6. Hội thoại địa điểm và quyền của model

Model hiểu mục đích, tên/vùng/loại địa điểm, một chiều/khứ hồi, và có thể đề xuất ứng viên hợp lệ. Parser fallback dùng cùng semantic contract. Server kiểm tra ứng viên có thật, handle/revision hiện hành, nguồn trung tâm và khả năng tính tuyến. UI và simulator không gọi classifier để hiểu lại action bấm nút.

`PendingQuestion` cần có ID, loại, target ID/revision, các câu trả lời hợp lệ và hành động tương ứng:

- `accept_place`: “Đúng chỗ này chứ?” → đồng ý chọn handle, chưa chạy.
- `confirm_destination`: “Bạn xác nhận dùng điểm này nhé, hoặc nói địa điểm muốn đổi.” → “OK”, “xác nhận”, “giữ địa điểm”, “có” giữ điểm đề xuất; “không”, “đổi địa điểm” hỏi tên mới. Chưa khởi hành.
- `change_destination`: tương thích checkpoint cũ hỏi “Bạn muốn đổi điểm cụ thể khác không?” → “không” giữ điểm; “có” hỏi tên mới. “OK”, “xác nhận”, “giữ địa điểm” vẫn giữ điểm.
- `choose_place`: “Bạn chọn chỗ nào?” → số thứ tự/tên/handle.
- `place_area`: bổ sung khu vực cho POI chưa tìm được, giữ tên POI trong `clarification.query`; không đổi thành điểm đại diện tỉnh/thành.
- `provide_origin/destination`: bổ sung slot, chưa xác nhận.
- `start_plan/replace_route`: đồng ý chấp nhận đúng offer, revalidate rồi thực hiện.

Một lượt chỉ có một câu hỏi foreground. Nếu multi-task đang hỏi nhiệt độ còn thiếu, offer bắt đầu tuyến vẫn có trên thẻ nhưng không đồng thời nhận “ừ” của câu hỏi nhiệt độ. Câu bắt đầu có đích rõ hoặc bấm nút target có thể chấp nhận offer trực tiếp.

| Câu nói/tình huống | Hành vi |
|---|---|
| “Đi Ninh Bình” | Xác minh trung tâm Hoa Lư phù hợp, tính dự thảo; hỏi có đổi điểm cụ thể không. Nếu chưa xác minh được thì hỏi địa chỉ |
| “Không” sau hỏi đổi điểm | Giữ trung tâm, trình bày kết quả và đề nghị bắt đầu; không huỷ và không chạy |
| “Có” sau hỏi đổi điểm | Chỉ hỏi địa điểm mới |
| “Tràng An” | Tìm trong vùng đang nói tới; sửa đúng draft |
| Tên riêng khớp rõ với một ứng viên | Chủ động đề xuất/tính trước; điểm vẫn hiện rõ trong tóm tắt và bắt đầu cần chấp nhận |
| Một ứng viên hợp lý nhưng chưa chắc đúng đích | Hỏi “Đúng chỗ này chứ?”; chưa chuyển sang start offer |
| Nhiều ứng viên tương đương | Hiện tối đa ba ô phân biệt bằng tên + địa chỉ ngắn |
| “Trạm sạc gần nhất” | Tìm đúng loại, kiểm tra khoảng cách lái và khả năng tới; đề xuất kết quả tốt nhất, chưa tự chuyển tuyến |
| “Lập kế hoạch tới Thanh Hoá và quay lại” | `trip.plan`, giữ hai chiều/SOC liên tục; trình bày kết quả trước offer bắt đầu |
| Không tìm được kết quả/tuyến đủ phù hợp | Hỏi thêm hoặc incomplete; không cho nút Bắt đầu khả dụng |

Lời nói không đọc lại toàn bộ thẻ: tên + địa chỉ tới phường khi cần phân biệt; không đọc lặp cả câu tóm tắt ở capability và frontend. Mỗi response có một nguồn speech và ID để chống phát TTS lặp. Mở bản đồ/bấm thẻ không đọc lại kết quả nếu không có thông tin mới.

“Dừng lại/tạm dừng” ánh xạ pause; “kết thúc hành trình/không đi nữa” ánh xạ end; “huỷ tìm địa điểm” ánh xạ cancel draft. Nếu chỉ nói “huỷ” khi đồng thời có draft và active journey, ưu tiên câu hỏi foreground có scope rõ; nếu chưa xác định được thì hỏi một câu, không huỷ cả hai.

## 7. Invariant và xử lý cập nhật

1. Một ActiveJourney; route/đích/ETA/3D/mini map cùng ID/revision.
2. Đổi draft, chọn địa điểm, mở map, ẩn thẻ không sửa ActiveJourney.
3. Moving cần gear D; paused/charging/completed/ended cần P trong simulator. Dữ liệu không khớp phải reconcile server, không chỉ đổi nhãn UI.
4. Action gửi event ID + target ID/revision; confirmation còn gắn offer ID và dấu điều kiện tính. Dùng đúng một lần; kết quả replay trả trạng thái hiện hành, không phát lại start hoặc UI event cũ.
5. Nút disable ngay trong lúc gửi. Sau thành công, offer/grant đã dùng bị loại. Response cũ không khôi phục card hoặc nút đã mất hiệu lực.
6. Gear event và tiến trình dùng một bộ điều phối session. Response chứa state snapshot đồng bộ của vehicle/draft/journey; frontend nhận một lần rồi render. Cập nhật progress không còn tự ghi gear để chữa state.
7. Bản preview pin/vị trí thay đổi đáng kể thì tính lại và hỏi chấp nhận revision mới trước khi start/replace. Nếu pin không đủ hoặc API lỗi thì giữ hành trình hiện tại.
8. Khi restart không tự phát lại lệnh hoặc bắt đầu simulation từ checkpoint. Reconcile vehicle/position, phục hồi thành trạng thái cần resume/chấp nhận hợp lệ.
9. Frontend renderer thuần trình bày; không gọi pause/start/PUT driving. Animation chỉ báo tiến trình của journey đã được server chấp nhận.
10. Hành trình kết thúc được lưu thành kết quả; xoá history sản phẩm không làm mất ledger chống replay.

## 8. Kịch bản nghiệm thu tập trung

| Ca | Chuỗi thao tác | Kết quả bắt buộc |
|---|---|---|
| S01 | P → tìm chỗ → chọn → xem map → đóng | Xe vẫn P; home không bị thu hẹp; không tự chạy |
| S02 | Ready → bấm Bắt đầu hai lần/response lặp | Một start; cảnh 3D; nút/grant cũ không còn tác dụng |
| S03 | Ninh Bình → không | Giữ trung tâm, một câu hỏi bắt đầu; không huỷ |
| S04 | Ninh Bình → có → Tràng An → đúng | Đúng chấp nhận địa điểm nếu đang hỏi địa điểm; chưa chạy |
| S05 | Đang đi Hồ Tây → tìm Vinhomes → huỷ | Cảnh/ETA vẫn Hồ Tây; draft biến mất |
| S06 | Đang đi Hồ Tây → Vinhomes ready → chấp nhận thay tuyến | 3D/map/ETA đổi cùng revision từ vị trí hiện tại; không còn đường cũ |
| S07 | Đang đi A → P → D | Tiếp tục A đúng vị trí và SOC; không start lại từ đầu |
| S08 | A paused + B ready → D | Tiếp tục A; B chỉ chạy/thay tuyến khi được chấp nhận rõ |
| S09 | Lập lộ trình sạc khứ hồi → từ chối bắt đầu | Kế hoạch xem lại được; xe đứng yên; không mất chiều về |
| S10 | P + draft ready chưa chấp nhận → D | Demo tự do, không tự bám draft |
| S11 | P pause thành công → kết thúc/bỏ kế hoạch | Không báo sai phải chuyển P; state vehicle và journey khớp |
| S12 | Ẩn thẻ/mini map → progress → P → mở từ dock | Không tự hiện lại; mở đúng đối tượng; không đổi gear |
| S13 | Charging → D trước/sau mức pin rời trạm | Trước thì chặn; sau thì tiếp tục cùng journey; không reset pin |
| S14 | API lỗi/thiếu trạm/thiếu đường về | Incomplete, không Bắt đầu, giữ tuyến đang chạy |
| S15 | Tăng điều hoà và lập kế hoạch/nhạc | Giữ bước còn thiếu; start offer không nuốt “ừ” của câu hỏi khác; bước sau tiếp tục khi đủ slot |
| S16 | Kết thúc → D → xem lịch sử → xoá kết quả | D không tự resume tuyến đã ended; history sản phẩm và ledger độc lập |

Replay riêng 1026–1034 và kịch bản Hồ Tây/Vinhomes. Kiểm tra thật ở trình duyệt trên màn hình rộng/hẹp: bấm, scroll, ẩn/mở lại, 3D khi start và P/D. Mock chỉ dùng để xác minh chuyển trạng thái và replay; không kết luận UX từ mock.

## 9. Thứ tự thực hiện đề xuất

1. Thay contract trộn trạng thái bằng PlanDraft/ActiveJourney và PendingQuestion; chuẩn hoá các transition/gear event trong một bộ điều phối, giữ API công khai tương thích. Migration checkpoint cũ không tự resume; giữ history và ledger.
2. Frontend nhận domain snapshot và tạo một view model; tách animation/map adapter khỏi UI renderer. Loại callback đổi gear khi render và nút start chung theo biến mapTripPlan.
3. Thay các panel/nút lặp bằng một thẻ foreground và workspace Chỉ đường; hoàn thiện Home P, Driving 3D, Map chi tiết và vòng đời ẩn.
4. Chuẩn hoá prompt/parser theo loại câu hỏi; dùng cùng transition cho giọng nói và typed UI action. Quyết định đề xuất/chọn điểm không cấp quyền start.
5. Chạy các ca tập trung ở trên và kiểm tra UI trực tiếp. Chỉ sau khi vòng đời ổn định mới hoàn thiện danh sách lịch sử hành trình và xoá lịch sử sản phẩm.

Không cần thay lại VietMap/thuật toán năng lượng/cảnh 3D trong đợt này. Việc thiếu hiện tại là contract hành vi thống nhất từ backend sang UI.
