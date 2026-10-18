# Benchmark agent theo tính năng

Benchmark chạy trên **agent dùng model Jetson** và **agent dùng Gemini**. Mỗi nhóm
input và mỗi nhóm output có thư mục riêng. Sưởi ghế nằm ngoài phạm vi; nhạc chỉ
kiểm tra **bật/tắt đoạn nhạc cố định**, không chọn bài/ca sĩ/thể loại/radio.

```text
cases/
  doors/             # mở/đóng/khóa/mở khóa cửa
  windows/           # nâng/hạ, phần trăm mở, vị trí
  music/             # bật/tắt đoạn nhạc cố định
  climate/           # đặt, tăng/giảm nhiệt độ, thiếu số độ
  body_panels/       # capo, cốp, toàn bộ cửa/capo/cốp
  vehicle_status/    # pin, lốp, nhiệt độ, cửa, kính...
  navigation/        # tìm/chọn điểm, lập tuyến, khứ hồi, P/D, start/stop/end
  information/       # cẩm nang, thời tiết, web, kiến thức
  memory/            # nhớ/đọc/quên/reset
  dialogue/          # câu hỏi, hội thoại
  hard_cases/        # thiếu thông tin, ngữ cảnh cũ, ý định dễ nhầm
  multi_tool/        # phối hợp hai tool, câu tuần tự và đồng thời
  adversarial/       # phủ định, đổi ý, điều kiện, cố vượt xác nhận
```

Mỗi thư mục chứa `language.jsonl`, và `tools.jsonl` / `workflows.jsonl` nếu phù hợp.
[Inventory](cases/README.md) ghi số ca, tool và kiểu input từng tính năng.
`cases/manifest.json` lưu thống kê chi tiết. Thứ tự chạy là các tính năng thường,
sau đó **hard_cases → multi_tool → adversarial**; chỉ đảo thứ tự bên trong nhóm.

## Chạy hai agent

Chạy lệnh từ gốc repo. Provider `local` dùng endpoint/model Jetson trong cấu hình;
`google` dùng Gemini đã cấu hình. API key không nằm trong dataset hoặc report.

```bash
# Kiểm tra toàn bộ dataset, coverage và phạm vi đã loại trừ
.venv/bin/python -m benchmarks.agent.run --validate-only

# Cùng input, so sánh Jetson và Gemini, output chia theo agent/tính năng
.venv/bin/python -m benchmarks.agent.run --mode live --provider both \
  --suite language --output benchmarks/agent/results/comparison

# Chạy thử tất cả nhóm, mỗi nhóm 2 ca, vẫn giữ nhóm khó ở cuối
.venv/bin/python -m benchmarks.agent.run --mode live --provider both \
  --suite language --samples-per-feature 2

# Chạy riêng cửa hoặc câu ghép trên cả hai agent
.venv/bin/python -m benchmarks.agent.run --mode live --provider both \
  --suite language --feature doors
.venv/bin/python -m benchmarks.agent.run --mode live --provider both \
  --suite language --feature multi_tool

# Test agent thực thi trên xe mô phỏng, mỗi agent có DB riêng
.venv/bin/python -m benchmarks.agent.run --mode live --provider both --suite tools

# Baseline offline: proposal viết tay qua agent/xe/policy thật, không gọi model
.venv/bin/python -m benchmarks.agent.run --mode fixture --quiet
```

## Chạy đầy đủ Jetson và xuất HTML

```bash
.venv/bin/python -m benchmarks.agent.run --mode live --provider local \
  --suite all --concurrency 2 --quiet \
  --output benchmarks/agent/results/jetson-complete
.venv/bin/python -m benchmarks.agent.html_report \
  --report benchmarks/agent/results/jetson-complete/report.json
```

Lượt đầy đủ hiện có **834 ca live** (102 ca agent/xe mô phỏng, 732 ca ngôn ngữ).
Ba ca cố ý đưa model output lỗi/mất provider chỉ chạy fixture; 17 workflow cũng
chạy offline. Lượt fixture đầy đủ có 122 ca, trong đó 102 ca trùng với phần live;
hai lượt phủ đủ **854 ID ca riêng biệt**, không cộng hai điểm thành độ đúng model.
Runner exit 1 khi có ca fail; vẫn chạy lệnh xuất HTML sau khi runner kết thúc.

Theo yêu cầu hiển thị mới, báo cáo bàn giao dùng HTML **chỉ mô tả lỗi**;
output/expected đầy đủ giữ trong JSON. Sau khi các lượt `jetson-v13-complete`,
`offline-v13-complete`, `jetson-v13-short-turns`, `offline-v13-short-turns`,
`jetson-v13-position-phrases`, `offline-v13-position-phrases` hoàn tất, chạy:

```bash
PYTHONPATH=. .venv/bin/python scripts/summarize_agent_benchmark.py
```

Script so sánh với `jetson-final` (cùng 834 ca), xác minh hash dataset/source
và danh sách ví dụ prompt rỗng, rồi xuất `jetson-v13-complete/analysis.html`
và ZIP. Có thể đổi thư mục bằng các cờ `--current`, `--baseline`, `--offline`,
`--supplemental`, `--supplemental-offline`, `--positions`, `--positions-offline`.
Các corpus bổ sung nằm ở `supplemental/short_turns.jsonl` (85 ca, giữ nguyên để
so sánh) và `supplemental/position_phrasings.jsonl` (8 cách nói vị trí, kiểm giá
trị trong task, payload xác nhận và đúng cửa thay đổi sau đồng ý). Các bộ này
chạy qua `scripts/benchmark_agent_supplemental.py --mode live|fixture`;
chọn bộ thứ hai bằng `--cases`. Không cộng điểm fixture vào độ đúng model.

Generator cũ `benchmarks.agent.html_report` dưới đây vẫn cung cấp chế độ
chi tiết khi cần đọc trace. `analysis.html` chứa thống kê, phân tích nhóm lỗi và bộ lọc theo input/tính năng.
`failures/<tính năng>/*.html` và `.json` chứa nguyên văn bài test (kể cả ngữ cảnh,
trạng thái ban đầu, expected), output đầy đủ từng lượt, trace, các assertion fail
và giải thích dựa trên giá trị kỳ vọng/thực tế. Giữ cả lượt trước đó trong ca
nhiều lượt để đọc được ngữ cảnh. Generator từ chối gắn bài test nếu hash dataset
không khớp với lúc inference. HTML dùng tài nguyên nội tuyến, mở được offline.

`--provider local` / `google` chạy riêng một bên. `--concurrency N` chạy tối đa N ca độc lập trong cùng nhóm trên mỗi agent (mặc định 1); latency khi chạy batch gồm cả thời gian chờ.
Gemini nghỉ 3 giây giữa các ca theo mặc định (`--gemini-delay`); dùng
`--concurrency 1` khi cần hạn chế tốc độ. HTTP 429 được thử lại tối đa hai lần,
cách nhau 30 giây, với trạng thái ca được tạo mới; toàn bộ lượt bị giới hạn vẫn
được giữ trong `rate_limit_retries`. Lỗi còn lại có `service_error=http_429`,
không được diễn giải thành lỗi hiểu ngôn ngữ.

`--resume` với cùng `--output` tiếp tục từ `progress.jsonl`, giữ cả ca pass và
ca fail nghiệp vụ đã chạy, chỉ gọi lại ca chưa hoàn tất hoặc bị HTTP 429.
Runner kiểm hash source/dataset, model, transport và danh sách ca trước khi
tiếp tục. Chạy mới phải dùng thư mục mới để tránh trộn kết quả hai lần chạy.
`--repeats N` lặp với trạng thái
mới; `--seed` quyết định thứ tự trong nhóm; `--case` nhận glob. `--limit` giới hạn
tổng số ca, còn `--samples-per-feature` giữ mẫu ở **từng** tính năng. Mặc định tạo
thư mục mới cho mỗi lần chạy; `--output` chọn đích cụ thể.

```text
results/<lần chạy>/
  comparison.json
  comparison.md
  jetson/
    index.md
    doors/language/report.json
    doors/language/report.md
    windows/language/...
    music/language/...
    hard_cases/language/...
    multi_tool/language/...
    adversarial/language/...
  gemini/
    index.md
    doors/language/...
    ...
```

Chạy một agent hoặc fixture thì các thư mục tính năng nằm ngay dưới thư mục
output, không thêm tầng `jetson/gemini`. Có `report.json`, `report.md`, `index.md`
tổng hợp; report từng tính năng chỉ tính metric của chính tính năng đó.

## Input và oracle

Mỗi nhóm có nhiều cách diễn đạt: trực tiếp, lịch sự, khẩu ngữ, câu hỏi, câu gián
tiếp, không dấu, lỗi gõ, số bằng chữ, sửa ý và ngữ cảnh nhiều lượt. Các ca khó kiểm
tra khác biệt cửa/cửa kính, mở cửa/mở khóa, nhiệt độ đích/độ thay đổi, đọc trạng
thái/tra cẩm nang. Nhóm phối hợp có cả câu `và` lẫn `rồi`, nhiều cặp tính năng khác nhau.

Hai model dùng định dạng output khác nhau, nên **điểm so sánh chính dùng cùng
quyết định nghiệp vụ và tham số**, không ép Gemini sinh tên alias riêng của SLM.
Điểm đến dạng object và chuỗi query cùng địa danh được chấm tương đương;
đổi địa danh hoặc thêm ràng buộc vị trí khác vẫn fail.
Oracle này được viết từ ý định tool đã định nghĩa trong benchmark, được đóng băng
trong JSONL; không tạo expected từ output model hoặc adapter production.
[oracles.py](oracles.py) mô tả phép chuẩn hóa độc lập.

Raw tool call, semantic, proposal đầu/cuối, repair/fallback và lỗi validation vẫn
được lưu. `local_tool_expect` kiểm thêm chi tiết alias/argument của Jetson như một
chẩn đoán riêng. Câu đồng thời cho phép đổi thứ tự hai call, nhưng thiếu/thừa call
hoặc sai tham số vẫn fail. Câu `rồi` kiểm đúng thứ tự. Không tính fallback thành
model hiểu đúng; model/provider được phục vụ ghi trong từng attempt.

- `language`: interpreter thật, không thực thi capability.
- `tools`: model thật (live) hoặc raw proposal viết tay (fixture), đi qua agent,
  validation, task, confirmation và xe mô phỏng; kiểm trạng thái trước/sau.
- `workflows`: semantic/event viết tay để kiểm lifecycle, navigation, replay,
  collision, restart, stream và consent cũ.

Mỗi ca có oracle độc lập trong `expect`. Hỗ trợ scalar/list và `contains`,
`not_contains`, `length`, `min`, `exists`, `one_of`, `same_members`, `same_calls`,
`decisions`, `ordered_decisions`. Lượt fixture có `fixture_attempts`: mỗi mảng là
một inference, kể cả bước discovery/repair. Lượt xác nhận host không gọi model có
mảng rỗng. Expected không được tự cập nhật từ kết quả chạy.

## Phạm vi thực thi

Xe, map, history, memory, checkpoint và ledger luôn là fixture/DB tạm từng ca.
Không gọi backend đang chạy, không dùng MQTT hoặc xe thật, không đọc DB phiên
người dùng. Live mode chỉ gọi các model được chọn. RAG/web/thời tiết/hội thoại có
ca dependency lỗi và ca thành công với HTTP mock/bằng chứng tổng hợp; không đo
chất lượng nguồn Internet hay model viết câu trả lời trong những ca này.

Report có điểm theo tính năng/kiểu input, raw output, trạng thái xe trước/sau,
repair/fallback, p50/p95 lượt và model attempt, token/timing khi server cung cấp,
revision/dirty, hash source/dataset. Nếu source/dataset đổi trong lúc chạy, CLI
exit 1 và đánh dấu report. Kịch bản fail cũng exit 1; dataset/CLI sai exit 2.

Khi sửa scorer, có thể chấm lại raw output mà không gọi lại model, không đổi
expected. Lệnh dưới giữ report gốc, hash code lúc inference và ghi thêm hash
code lúc chấm; từ chối nếu dataset đã thay đổi:

```bash
.venv/bin/python -m benchmarks.agent.rescore \
  --input benchmarks/agent/results/comparison \
  --output benchmarks/agent/results/comparison-rescored
```

Đây là corpus **development**, chưa phải holdout được review độc lập. Một phần
ca kế thừa từ `eval/` được ghi tiền tố nguồn trong ID. Oracle workflow
`offline-compound-preserves-task` theo contract hiện tại: giữ task nhưng bỏ câu
hỏi xác nhận cũ khi lượt mới là câu ghép chưa xử lý được. Fixture score không phải
độ đúng model; STT/TTS, browser, MQTT và phần cứng nằm ngoài bộ text-agent này.
