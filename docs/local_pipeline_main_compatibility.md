# Giữ hợp đồng main, chỉ mở rộng runtime local

Baseline đối chiếu: `origin/main` tại `9c76585d1d899e647ff8e0772931ccdf9989e814`.
Tài liệu này thay thế mô tả hành vi đang chạy trong báo cáo audit chốt ở `94c9223`.
Báo cáo audit cũ vẫn là ảnh chụp lịch sử của commit đó.

## Phần khôi phục nguyên main

Các file sau được khôi phục nội dung giống hệt baseline:

- `app.js`, `index.html`: UI, STT → turn streaming, mic, follow-up và playback.
- `monitor.js`, `monitor.html`.
- `src/vivi/dialogue/parser.py`: toàn bộ grammar/guard có sẵn trên main; không giữ guard mới.
- `src/vivi/conversation/service.py`: instruction và cách dựng history/question của answer generator.
- `src/vivi/domain/models.py`: giới hạn public TurnRequest 500 ký tự như main.

Ở planner, giữ nguyên main cho:

- `SEMANTIC_INSTRUCTION` và `PROMPT_VERSION`.
- `VOICE_PERSONA + SEMANTIC_INSTRUCTION + TOPIC_INSTRUCTION + suffix`, cả local và cloud.
- Tool `interpret_turn`, `strict_schema(semantic_schema())`, slot definitions và JSON repair.
- Thứ tự user prompt, catalog đầy đủ, sáu query/answer lịch sử không bị cắt 500/700 ký tự.
- Cách đưa task/history vào context, guard/fallback/circuit breaker của main.
- Output limit `planner_max_tokens`, hiện là 2048; không ép local xuống 512 khi schema main đòi đầy đủ trường.

Không còn local instruction mới, schema oneOf theo capability/act, compact history ba lượt
hoặc thay đổi grammar để bù cho lỗi lựa chọn tool của Qwen.

## Hai đường runtime

### Cloud và rules

- Đi qua UI/route cũ của main, không đi qua voice inbox mới.
- Semantic deadline vẫn dùng `semantic_budget_seconds` như main.
- Toàn lượt vẫn dùng `turn_timeout_seconds` như main.
- Client/provider cloud, schema và lịch sử giữ baseline; không kế thừa timeout dài của local.

### Local với AI server

- Cùng prompt/schema main; chỉ dùng ngân sách chờ local riêng để chịu được inference 30–60 giây.
- `ai_server_request_timeout_seconds`: mặc định 180 giây ở client/semantic budget.
- Turn timeout riêng cho local: tối thiểu `2 * local_request_budget_seconds + 15`.
- Cloud/rules không bị kéo dài timeout toàn lượt vì cấu hình này.
- Warmup local chạy nền, dùng cùng system/tools prefix và catalog thực; warmup không thực thi action.
- Jetson tiếp tục sử dụng scheduler: LLM lane riêng; STT/TTS speech lane chung theo policy mặc định.
- Giữ cancellation khi client rời đi, cleanup upstream, bounded queues, no-context STT, RAM guard và các profile TTS.

Không thay model hoặc inference backend. UI không được viết lại thành luồng hội thoại riêng cho local.

## Hành vi UI đã xác minh

Main không khóa input trọn lượt:

- Nút gửi và một số control bị khóa khi busy.
- Mic vẫn có thể bấm; bắt đầu lượt mới có thể ngắt lượt đang chạy.
- Đây là lựa chọn người dùng đã xác nhận giữ nguyên.

Vì UI khôi phục main, UI mặc định không dùng cơ chế gộp nhiều voice input mới của nhánh.
API buffering local vẫn còn để thử riêng, nhưng mặc định **tắt**:

```toml
local_voice_buffering_enabled = false
```

Chỉ khi bật tùy chọn này mới khởi động worker inbox và phục hồi pending jobs. API intake
chỉ nhận `llm_provider="local"`; route cloud cũ không đổi. Nếu local failover sang cloud,
service không thêm debounce 450 ms dành cho local. Transcript trong inbox không bị xóa khi tắt tính năng.

Snapshot local dài dùng subclass `VoiceTurnRequest` riêng, tối đa 10.000 ký tự;
không mở rộng giới hạn API turn chung của main.

## Kiểm chứng và giới hạn

- Fixture `tests/fixtures/semantic_main_9c76585.json` lưu nguyên system/tool contract của baseline.
- Test kiểm tra cả local và cloud nhận đúng system prompt và sáu lượt lịch sử đầy đủ.
- Test riêng deadline local/cloud, public turn limit và opt-in local inbox.
- Test voice tiếp tục nói khi inference đang chạy được giữ ở `tests/test_voice_inbox.py`;
  đây là test của API buffering tùy chọn, không phải hành vi UI main mặc định.
- Benchmark planner dùng đúng output limit runtime và ghi riêng `raw_correct`, `final_correct`,
  provider được dùng, raw outputs và các lần guard/fallback; không assert rồi coi model luôn đúng.
- Output cuối đúng nhờ guard hoặc rules không được tính thành LLM đúng.
- Có một test UI có sẵn trên main thất bại: regex của
  `test_frontend_confirmation.cjs` chưa tính tham số `questionReply` trong chữ ký hàm.
  Đã tái hiện bằng nguyên `app.js` và test từ main; không sửa UI main để chiều regex đó.

Giữ nguyên prompt main là khôi phục một hợp đồng, không phải bằng chứng Qwen tự chọn đúng tool.
Độ chính xác raw model và thời gian xử lý cần đọc riêng trong benchmark.


## Probe trên Jetson sau khôi phục prompt main

Dữ liệu: [jetson_planner_main_contract_probe_2026-10-15.json](jetson_planner_main_contract_probe_2026-10-15.json).
Chỉ một cold run và một request kế tiếp, không phải benchmark 30 warm.

- Câu: “mở cốp xe”; model và backend hiện có, đúng system/tools main, output limit 2048.
- Cold run: 88.589 ms, gồm inference và một lần repair JSON; raw model chọn
  `door.set_open`, không phải `trunk.set_open`. Validation không chấp nhận proposal.
- Kết quả cuối đúng do fallback rules, không phải Qwen hiểu đúng.
- Request kế tiếp: 1,12 ms, provider rules, circuit local đang mở; **không gọi model**.
  raw_correct được ghi null, không ghi là một lần model trả sai hay đúng.
- Không dùng con số 1,12 ms để tuyên bố tối ưu SLM. Token usage trong report là response
  backend cuối của lượt, không phải tổng tất cả lần repair.

Kiểm chứng phần thay đổi: 257 test Python liên quan pass (các bộ có phần giao nhau đã
được tính một lần); ba file test frontend pass. Một test confirmation cũ trên main
vẫn fail do regex như đã nêu. Không sửa model/prompt để làm đẹp kết quả probe.
