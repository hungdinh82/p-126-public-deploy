# Quản lý hội thoại theo tác vụ — 07/10/2026

## Kết luận từ log

Các lượt 625–633 trong `data/vivi_rag.sqlite3` cho thấy mất trạng thái tác vụ:
“buồn quá” → đề nghị nghe nhạc → “có” lại hỏi người dùng muốn gì; xoá thông tin
cá nhân bị đưa vào cẩm nang; “phát nhạc đi” gửi tên bài là “đi”; câu trách
“sao bạn không mở nhạc luôn đi” bị hiểu là cấm phát nhạc. Đây chủ yếu là lỗi
điều phối hội thoại/NLU trước truy xuất. Chỉnh ranking RAG không giải quyết được.

## Cơ sở thiết kế

- [LiveKit Tasks](https://docs.livekit.io/agents/logic/tasks/): tác vụ có mục tiêu,
  kết quả có kiểu, giữ quyền xử lý trong một khoảng hội thoại rồi trả lại điều khiển.
- [Rasa Dialogue Understanding](https://learning.rasa.com/rasa-pro/dialogue-understanding/):
  tách hiểu ngôn ngữ khỏi quản lý luồng công việc có trạng thái.
- [Qwen Function Calling](https://qwen.readthedocs.io/en/latest/framework/function_call.html):
  dùng định dạng function calling mà model được huấn luyện để lựa chọn công cụ.

Repo áp dụng các nguyên tắc đó trong LangGraph/SQLite hiện có; không cài thêm
LiveKit/Rasa. Planner có thể chạy local hoặc qua provider cloud đã cấu hình;
các workflow xác định vẫn quản lý trạng thái và thực thi tác vụ.

## Phần đã xây

`agents/tasks.py` lưu task tại `diagnostics.pending_task` của lượt hoàn tất cuối
cùng trong SQLite history. Bốn loại: offer, slot, vehicle_confirmation, memory_reset.
Mỗi task có origin_turn, expires_at, tham số đã biết; tác vụ thường hết hạn sau
3 phút, xác nhận xe dùng hạn do gateway cấp. History đã được tách theo session/profile.

“Có/không” chỉ tiếp tục hoặc huỷ task còn hạn. Đổi chủ đề ghi lượt mới không có task,
không hồi sinh lời mời cũ. Trạng thái được ghi rõ resumed/confirmed/cancelled/
superseded/expired. Chỉ offer có cấu trúc mới trở thành tác vụ; không đoán hành động
từ một câu trả lời tự do trong log. Lịch sử cũ thiếu frame không tự cấp quyền.

Slot zone/temperature/level hoàn thiện tham số đã lưu, rồi đi qua cùng validator
và gateway. Đồng ý mở cửa sổ vẫn phải qua confirmation thật; trạng thái xe thay đổi
sau lúc hỏi sẽ bị gateway từ chối. Xác nhận gateway nằm trong bộ nhớ tiến trình:
restart backend khiến xác nhận xe cũ không còn hợp lệ, cần yêu cầu lại.
Offer và slot có thể khôi phục từ SQLite sau restart.

`agents/tools.py` có 13 tool domain; bổ sung recall/remember/forget/reset cho memory.
`memory/tools.py` thực hiện transaction và tạo câu trả lời từ kết quả thực tế.
Reset do model đề nghị hoặc câu xoá thông tin cá nhân mơ hồ phải chờ câu đồng ý.
Cú pháp cũ tường minh “xoá toàn bộ bộ nhớ” vẫn giữ hành vi xoá trực tiếp đã có.
Các thử nghiệm dùng database tạm, không xoá bộ nhớ người dùng trong data/.

Local provider dùng native functions (`agents/native_tools.py`) với bốn hành vi
hội thoại reply/offer/clarify/unsupported. Tên function được chuyển về intent nội bộ;
tham số vẫn kiểm tra bằng registry. Chỉ một function mỗi lượt, không nhận function
lạ hoặc nhiều tool call để tránh thực thi một phần yêu cầu. Model timeout/network
failure chỉ thử một lần ở local, ghi lỗi rồi dùng rules fallback.

`agents/authorization.py` kiểm tra ranh giới trước executor: lệnh phủ định và câu
đồng ý không gắn với task không được gọi model để suy diễn thành hành động; model
không được tự chọn zone còn thiếu; phát nhạc suy ra từ tâm trạng phải thành offer;
ghi memory phải có yêu cầu lưu. Đây là các chặn xác định cho trường hợp đã hỗ trợ,
không phải bộ chứng minh ngữ nghĩa cho mọi cách nói tiếng Việt.
Lệnh dừng nhạc tường minh cũng đi thẳng tới media.pause để không phải chờ model.

Rules còn là chế độ offline và fallback. Cấu hình hiện tạm chọn
[Google Gemini](google_api.md). Khi chọn local, rules không còn chặn
mọi lượt trước model. Task replies và memory requests tường minh đi thẳng theo
workflow đã biết. Đây chưa phải bộ thực thi nhiều hành động cùng lúc hay scheduler.

RAG tiếp tục dùng E5/SQLite và evidence gate. Bỏ tín hiệu kỹ thuật chỉ dựa vào từ
không dấu “cua”, tránh nhầm “của bạn” thành cửa xe. Memory không đi vào retriever.

## Chạy SLM local để thử

Đã tải Qwen3 1.7B Q4_K_M (1,107,409,472 byte) từ
[Unsloth GGUF](https://huggingface.co/unsloth/Qwen3-1.7B-GGUF), revision
`d7f544eead698dbd1f15126ef60b45a1e1933222`. SHA-256 khớp metadata LFS:
`b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897`.
Model gốc: [Qwen3 1.7B](https://huggingface.co/Qwen/Qwen3-1.7B).
Runtime llama.cpp b11447 tải từ release chính thức, kiểm checksum release.

Provision một lần có mạng:

```bash
./scripts/setup_local_slm.sh cpu
# Trên Jetson: tải model, dùng binary build phù hợp JetPack/CUDA của thiết bị.
./scripts/setup_local_slm.sh model-only
```

Inference không tải file và chỉ bind loopback:

```bash
./scripts/run_local_slm.sh
# Terminal khác, chạy benchmark biệt lập trước:
LOCAL_LLM_MODEL=vivi-qwen3-1.7b LLM_TIMEOUT_SECONDS=90 \
  .venv/bin/python -m eval.voice_benchmark --provider local \
  --dataset eval/task_cases.jsonl --output eval/results/tasks-local-native-dev.json
# Khi chất lượng/độ trễ phù hợp, chọn provider local cho backend:
LOCAL_LLM_MODEL=vivi-qwen3-1.7b LLM_PROVIDER=local ./scripts/run_rag_local.sh
```

CPU mặc định dùng 4 threads, context 4096, 1 slot, tắt thinking. Có thể đặt
`LLAMA_SERVER_BIN`, `VIVI_SLM_GPU_LAYERS`, `VIVI_SLM_THREADS`,
`VIVI_SLM_CONTEXT`, `VIVI_SLM_PORT`. Backend phải dùng cùng port nếu thay đổi.

Máy đo hiện tại là WSL x86_64, i5-11300H, RAM khoảng 5.7 GiB, GTX1650 4 GiB,
không phải Jetson. Binary CUDA 12.8 gặp lỗi “PTX was compiled with an unsupported
toolchain” trên driver 560.94. Không thay driver hệ thống. Binary CPU chạy được;
chưa có phép đo GPU/Xavier hay đồng thời STT+TTS.

Bản structured JSON đầu tiên chọn sai ngay cả lệnh điều hoà; native tools chọn
được điều hoà và phát nhạc trong probe đầu nhưng còn lỗi hội thoại/đặt tham số.
Do đó chưa bật Qwen này làm mặc định. Native calling là thay đổi giao thức,
không phải bằng chứng model 1.7B đã đủ năng lực.

## Đánh giá

- `eval/task_cases.jsonl`: 6 episode, 14 lượt về offer, yes/no, slot, xác nhận xe,
  recall/reset memory và lời trách có lệnh. Kiểm cả execution và nội dung database.
- `tests/test_dialogue_tasks.py`: expiry, cancel, topic switch, profile/session
  isolation, restart, sync/async, không tin prose, an toàn sau thay đổi trạng thái xe.
- `tests/test_native_tools.py`: payload native, đúng một tool, validation, timeout.
- Benchmark voice hỗ trợ episode, bộ nhớ tạm riêng, p50/p95 mỗi lượt và error_count.
  Mạng bên ngoài bị chặn trong eval; xe dùng simulator.
- Kết quả rules: `eval/results/tasks-rules-dev.json`,
  `conversation-tasks-dev.json`, `voice-tasks-dev.json`.
- Kết quả SLM thật: `eval/results/tasks-local-native-dev.json`.
  Hai báo cáo `tasks-local-native-before-guard.json` và
  `tasks-local-native-before-stop.json` giữ nguyên những lần thử thất bại để tránh
  che mất lỗi phát hiện trong quá trình kiểm tra.

Lần chạy cuối: rules qua 6/6 task episode (14 lượt), 12/12 conversation episode
(18 lượt) và 57/57 voice case. Local native sau các chặn đạt route/intent đúng trên
6/6 task episode, nhưng còn **1 lỗi tham số** ở bài nhạc trong lời mời, dù người dùng
chưa chọn bài. p95 local trên CPU khoảng **62.6 giây**; p50 khoảng 40 ms vì nhiều
lượt yes/no/memory/stop đi thẳng qua workflow. Không dùng p50 này để đại diện tốc độ
model. Kết quả này chưa đạt điều kiện bật model làm mặc định; chất lượng hội thoại
tự do vẫn cần model tốt hơn và đo trên Jetson thực tế.

Kiểm tra cuối: **299 tests passed, 13 skipped**; Ruff, kiểm tra cú pháp JavaScript,
shell và `git diff --check` đều qua. Backend đã khởi động với rules/local-only;
smoke test qua HTTP xác nhận chuỗi buồn → đề nghị nhạc → có → bật nhạc → dừng nhạc
thực sự đổi trạng thái simulator. Không dùng profile memory thật để thử xoá.

Đây là development regression, có câu lấy từ log đã quan sát; không phải accuracy
trên tập held-out. Chất lượng câu trả lời thực tế, ASR nhiễu, ngắt lời/TTS và tài
nguyên khi chạy cả pipeline trên Xavier vẫn phải đánh giá riêng. UI hiện gửi tuần
tự; chưa có bảo đảm transaction cho nhiều client đồng thời trong cùng một phiên.
