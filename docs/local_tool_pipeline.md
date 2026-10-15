# Local: tool docs, discovery và native proposals

Bản cập nhật này thay phần **local** đã khôi phục tại `8ff1b7a`. Cloud và UI tiếp tục dùng main tại `9c76585`; không thay model, weights hoặc inference backend.

## AI có tài liệu tool gì?

Nguồn tool/executor vẫn là `ToolSpec` và `TOOL_REGISTRY` trong `src/vivi/agents/tools.py`. Local có một lớp tài liệu tại `src/vivi/dialogue/local_tools.py`, sinh từ registry đó:

- `name`: tên native function hợp lệ, ví dụ `trunk_set_open`.
- `description`: dùng khi nào, tránh nhầm công cụ nào, ý nghĩa tham số.
- `inputSchema`: chỉ tham số của tool đó; slot chưa biết có thể bỏ để reducer hỏi tiếp.
- `annotations.readOnlyHint`: thuộc tính thao tác, không phải quyền thực thi.
- `_meta`: capability nội bộ, chỉ đề xuất, từ vựng discovery, phép ánh xạ nếu có.

Tài liệu đầy đủ: [local_tool_catalog.json](local_tool_catalog.json). Đây là **định dạng tài liệu tương tự tools/list của MCP**, chưa phải MCP server/transport hoặc endpoint cho phép thực thi.

Xuất lại từ code:

```bash
PYTHONPATH=. .venv/bin/python scripts/export_local_tools.py --navigation --output docs/local_tool_catalog.json
# Bỏ --navigation nếu cần catalog không có bản đồ/hành trình.
```

Tham khảo: [MCP Tools](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx), [Qwen Function Calling](https://qwen.readthedocs.io/en/stable/framework/function_call.html).

## Đường xử lý local

1. Giữ nguyên transcript gốc ở đầu vào/lịch sử. Projection cho inference chỉ gộp đính chính rõ ràng của hai đoạn kề nhau, cùng một thao tác và cùng tập slot. Đoạn độc lập, đoạn chưa hiểu, phủ định và đính chính khác chế độ số được giữ nguyên. Dạng “đặt 26 độ” có thể kế thừa danh từ từ yêu cầu nhiệt độ tuyệt đối ngay trước nó; chỉ khi giá trị hợp lệ 16–30°C.
2. Discovery chọn tài liệu qua các cụm từ dương trong toàn bộ input. Đây không phải bộ quyết định action, giá trị hoặc quyền thực thi. Tool không khớp từ khóa không bị coi là không hỗ trợ.
3. Tối đa tám capability đầy đủ được đưa vào. Nếu danh sách ban đầu quá rộng, model nhận tool cơ bản và index tên/mô tả của toàn bộ catalog. `dialogue_find_tools` cho phép lấy thêm tài liệu, tối đa một lần. Query quá rộng được yêu cầu cụ thể lại; không tự dùng một tool gần nghĩa để thay thế.
4. Cả yêu cầu mới và lượt đang chờ bổ sung, Qwen gọi từng native function trực tiếp. Ví dụ `trunk_set_open({"open":true})`; không phải tự dựng toàn bộ `SemanticTurn`, task IDs, patches, flags và các slot không liên quan.
5. Với một capability dùng vị trí, schema giữ đúng vị trí được nói: không có vị trí thì không cho tự thêm zone; chỉ nói bên phải thì giữ target_side=right và để thiếu hàng ghế. Không áp dụng với nhiều vị trí/tool hoặc phủ định/loại trừ. Thiếu vị trí vẫn được giữ là thiếu; không mặc định ghế lái.
6. Adapter chuyển toàn bộ tool calls thành một `SemanticTurn` theo thứ tự, kiểm tra cả lô trước khi chuyển sang reducer. Đổi `zones:[một vị trí]` thành `zone` là chuẩn hóa tương đương. Không tự bỏ một action lặp hoặc thay action sai bằng action khác.
7. Backend vẫn sở hữu kiểm tra tham số, task/revision, consent, policy, execution ledger và TTS. `parallel_tool_calls=true` chỉ cho phép model đề xuất nhiều thao tác trong một response; không làm GPU chạy nhiều Qwen hay xe thực hiện song song.

Khi đã có task/câu hỏi đang chờ, local vẫn dùng native tools. Mỗi bước còn sửa được có tool update riêng, gắn handle/index và schema của capability đó; không quay về DTO `interpret_turn`. Cloud luôn đi đường JSON/prompt/history main, không kế thừa projection hoặc discovery của local.

## Các tool nhiệt độ

Một capability backend `climate.set_temperature` có hai cách biểu đạt cho model:

| Native tool | Đầu vào | Chuyển về backend |
|---|---|---|
| `climate_set_temperature` | `value_celsius` | Đặt nhiệt độ đích |
| `climate_adjust_temperature` | `direction`: increase/decrease; `amount` dương tùy chọn | Delta theo chiều; mặc định 2 độ |

Không thay executor hoặc model. Tool tương đối tách chiều điều chỉnh khỏi độ lớn; adapter mới chuyển thành delta có dấu. Không dùng hai tên warm/cool mà Qwen đã nhầm trong kiểm tra hội thoại. Giá trị mặc định là chính sách backend đã có, được ghi rõ trong tài liệu tool.

## Runtime và chống lỗi dây chuyền

- Local giới hạn output theo `min(planner_max_tokens, local_llm_max_tokens)`, hiện là 512.
- Tối đa một lần sửa JSON/validation và một lần lấy thêm tài liệu. Không retry vô hạn.
- JSON/proposal sai của một lượt không mở circuit local 30 giây, nên lượt sau vẫn gọi model. Timeout/lỗi transport vẫn mở breaker; cloud giữ chính sách main.
- Bỏ full-catalog warmup chạy nền ở startup ứng dụng: prefix đó khác shortlist thực và đã từng chen vào giữa inference/repair. AI server vẫn giữ model được load.
- UI, barge-in, streaming TTS và scheduler Jetson giữ như trước. Voice inbox vẫn opt-in; UI main không tự chuyển sang inbox.

## Cách đọc benchmark

`raw_tool_calls` là lời gọi nguyên bản. `raw_outputs` là proposal chuyển kiểu trước `parser.guard()`; có chuẩn hóa tương đương và mặc định của tool như đã công bố. `raw_correct` được đo **sau projection/ràng buộc schema local nêu trên**, không chứng minh model hiểu đúng input gốc khi không có các cơ chế đó. `final_correct` tách riêng kết quả sau guard/fallback.

Mỗi row lưu số backend calls, usage từng response, tổng output tokens và provider cuối. Không tính một lượt rules/circuit-open thành model trả lời nhanh. Các ca dùng input tổng hợp; không thực thi thao tác lên xe.

Không suy từ các regression case sang bảo đảm model luôn đúng với mọi câu nói. History của answer generator chỉ được rút gọn và đặt câu hỏi hiện tại ở cuối khi provider thực tế là local; cloud giữ thứ tự và sáu lượt đầy đủ của main.

## Kết quả lịch sử ở 717a549, 2026-10-15

Các số dưới đây đo contract cũ tại commit đó, không phải benchmark của bản sửa biểu mẫu bên dưới. Bộ 180 lượt chưa bao phủ trạng thái đang chờ bổ sung.

Cùng Qwen3-1.7B Q4_K_M, llama.cpp/CUDA hiện có, MAXN mode 0. AI server vẫn giữ STT/TTS đang chạy; TTS profile FP16 không đổi.

Baseline main được đo lại **không warmup**, không chủ động gửi request khác chen vào: “mở cốp xe” mất **82.970 ms**, hai response sinh **1.382 + 576 token**. Model sai tool/validation; kết quả cuối do rules. Dữ liệu: baseline độc lập (file sinh tại máy chạy: `jetson_main_planner_isolated_2026-10-15.json`; không lưu trong Git).

Bản mới: 30 warm mỗi ca, loại lượt đầu khỏi median/p95:

| Ca | Median SLM | p95 | Đúng trước guard |
|---|---:|---:|---:|
| Mở cốp | 616 ms | 688 ms | 30/30 |
| Đặt 24 độ | 691 ms | 702 ms | 30/30 |
| Mở kính bên tài | 812 ms | 855 ms | 30/30 |
| Đặt 24 → sửa 26 → thêm kính bên phụ | 1.395 ms | 1.404 ms | 30/30 |
| Lặp câu STT nghe không rõ | 828 ms | 849 ms | 30/30 |
| Than lạnh sau chủ đề đã hoàn tất | 669 ms | 681 ms | 30/30 |

Tổng 180/180 proposal trước guard đúng; không ca nào cần guard sửa trong bộ này. Tất cả là provider local, không tính rules thành model. Mỗi warm request dùng một backend call, 21–56 output tokens. Input 1.561–2.572 tokens. Lượt đầu từng ca khoảng 1,37–2,74 giây. Riêng mở cốp: input từ 10.388 token ở baseline xuống 1.561, output lần đầu từ 1.382 xuống 21.

Đây là độ trễ **semantic planner**, chưa cộng thu âm/STT/TTS/playback. Không lấy baseline một lượt so với median warm để tuyên bố hệ số tăng tốc thống kê. Report 30 lượt (file sinh tại máy chạy: `jetson_local_tools_benchmark_2026-10-15.json`; không lưu trong Git) ghi từng tool call, usage và phạm vi đo.

Đã probe thêm với catalog gồm cả bản đồ: mười ca, mỗi ca một lượt đầu và một lượt kế tiếp; gồm mở cốp rồi than lạnh, tăng nhiệt độ không nêu số, cốp+kính, và lệnh “đừng mở cốp”. Cả 20 lượt đúng trước guard. Đây là probe bổ sung, chưa phải 30 warm mỗi ca: report catalog đầy đủ (file sinh tại máy chạy: `jetson_local_tools_navigation_probe_2026-10-15.json`; không lưu trong Git).

238 mẫu tegrastats trong phiên benchmark: RAM 7.877–7.883 MB / 14.887 MB, swap đang dùng giữ ở 1.074 MB, GPU utilization tối đa 99%, nhiệt GPU 51,5–78,5°C. Đây là mẫu trong phiên, có khoảng nghỉ; không đo toàn bộ startup/model loading. Xung GPU đọc lúc idle là 114,75 MHz; không dùng số đó làm xung khi inference. Mode MAXN/0 đọc được; đọc EMC cap bằng nvpmodel báo thiếu quyền.

Kiểm thử: bộ regression Python 287 test pass; thêm regression catalog trống đã pass cùng bộ native/cloud/gateway (tổng 288 test khác nhau). Có test người dùng tiếp tục nói khi inference đang chạy, atomic proposals, discovery qua gateway có xác thực, cloud giữ main và history local riêng. UI không đổi; lỗi regex confirmation có sẵn trên main vẫn là giới hạn đã ghi ở tài liệu tương thích.


## Sửa lỗi biểu mẫu đang chờ và tool public

Contract `local-proposals-v2` dùng native tools cả khi có active task/clarification, thay nhánh cũ quay về `interpret_turn` khi đang chờ. Local không đưa câu hỏi trợ lý cũ vào prompt thao tác xe. Biểu mẫu khác capability không được dùng làm ngữ cảnh cho yêu cầu mới. Transcript/lịch sử gốc không bị xoá.

- `phía sau` bổ sung vào `target_side=right` của biểu mẫu cửa hiện tại. Runtime có lúc chỉ ghi status=waiting_slot mà không ghi missing_slots; local vẫn nhận thành phần vị trí từ câu trả lời ngắn, không gọi Qwen để đoán lại intent.
- Patch chỉ nhắm bước còn sửa được, đúng handle/index; không sửa bước completed/running/unknown. Khi lọc ngữ cảnh, giữ handle suspended gốc.
- Yêu cầu độc lập/đổi chủ đề đóng các biểu mẫu local chưa thực hiện và thu hồi xác nhận cũ. `OK` cũ không được mở lại cửa sau khi đã chuyển sang điều hoà. Completed effects và ledger giữ nguyên; cloud/UI giữ luồng main.
- Tool conversation không nhận `query` do model tự viết: host gắn utterance hiện tại. Clarification không được chỉ nhại lại câu STT.
- Một nhiệt độ tuyệt đối không có số hoặc tham chiếu giá trị trong input bị từ chối và yêu cầu model sửa một lần; không thay âm thầm thành action do parser viết. Các câu tăng/giảm/than nóng/lạnh có tool direction/amount riêng.

Đã đối chiếu các nút trong `app.js`, `journey.js` với registry: nhạc play/pause, điều hoà, kính, cửa/khoá, capo/cốp, cả thân xe và bản đồ đều có capability executor và tài liệu. Bổ sung hướng dẫn chi tiết media, sưởi ghế, body, cẩm nang, navigation và trip. Catalog JSON hiện xuất **cả navigation/trip**. Bật/tắt loa TTS là cài đặt trình duyệt (`handleSpeakerCommand`), không phải capability xe; nhạc trong demo là Web Audio mô phỏng, không phải dịch vụ phát bản thu theo tên bài.

Regression opt-in trên phần cứng, với xe giả lập và checkpoint tạm, không tác động phiên người dùng:

```bash
VIVI_TEST_JETSON_URL=http://100.123.27.125:8795 \
VIVI_TEST_FORM_REPORT=/tmp/jetson-forms.json \
.venv/bin/pytest -q -s tests/test_jetson_live_forms.py
```


### Đo lại sau sửa biểu mẫu

Báo cáo trước/sau (file sinh tại máy chạy: `jetson_forms_benchmark_2026-10-15.json`; không lưu trong Git) dùng cùng Qwen và endpoint 8795. Trước sửa (717a549), hai lượt `tôi lạnh quá` khi có clarification đều chuyển sai thành conversation với query là câu hỏi của trợ lý; lần đầu 15.352 ms, lần kế tiếp 1.830 ms. Đây là hai mẫu với trạng thái cache khác nhau, không dùng để suy tỷ lệ tăng tốc thống kê.

Sau sửa: 21 lượt graph thật trên xe giả lập (ba lần hai chuỗi), gồm mở biểu mẫu cửa, bổ sung hàng ghế, OK, hoàn thành rồi chuyển điều hoà, và chuyển điều hoà khi còn chờ xác nhận cửa. Tất cả assertion pass; không lượt nào guard sửa semantic. `phía sau` không gọi Qwen, tổng graph khoảng 54–81 ms; lượt cold tổng graph khoảng 1,53–1,61 giây. Xác nhận cửa cũ bị từ chối sau đổi chủ đề. Không lưu công việc trên Jetson.

Probe thêm 13 ca × hai lần (một lần đầu, một lần kế tiếp), gồm nhạc chung/nhạc thư giãn/dừng nhạc và các ca cũ: **24/26 proposal đúng trước guard, 26/26 kết quả cuối đúng**. Hai proposal sai là câu `đừng mở cốp`, được guard kế thừa main chặn; model chưa hiểu phủ định ổn định. Các ca nhạc warm khoảng 533–691 ms, tăng nhiệt độ không nêu số 774 ms. Không coi bộ nhỏ này là bảo đảm chất lượng mọi câu nói. Adapter chuẩn hoá danh từ `nhạc` thừa thành không có search query chỉ với lệnh nhạc chung; không đổi tên bài cụ thể.

223 test Python liên quan pass, cộng hardware regression nêu trên. Runtime khai báo jsonschema trong requirements và lock; cloud không import validator này trước khi cần adapter local. Không đổi model, inference backend hoặc UI.
