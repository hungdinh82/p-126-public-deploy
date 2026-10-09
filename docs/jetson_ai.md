# Jetson AGX Xavier: private STT / SLM / TTS server

Service này chỉ nhận yêu cầu suy luận. API `/api/v1`, LangGraph, RAG, history, safety và MQTT tiếp tục chạy trên Laptop. Profile PC/Nano cũ không đổi. Server có SLM Qwen3-1.7B Q4, STT Whisper-base và TTS ZeroTTS với giọng VIVI; trạng thái và thiết bị thực tế được báo riêng cho từng model. Router Laptop chọn provider trước khi chạy graph và phải bật rõ bằng env; chưa có fallback giữa lượt đang chạy.

## Giao diện theo dõi model và test API

Sau `bash scripts/jetson_ai.sh run`, mở `http://<jetson-private-ip>:8789/test` bằng URL mà script in. Giao diện nằm ngay trong private gateway, không cần chạy frontend riêng và không tải asset từ Internet.

- Mỗi thẻ STT/SLM/TTS hiển thị status, đường dẫn model, host/PID, engine CPU/GPU thực tế, thời gian load và thời gian xử lý gần nhất. Khi mất kết nối, status chuyển `offline`, không giữ nhãn `ready` cũ.
- Nhập text để test SLM hoặc TTS riêng. Upload audio để test STT riêng hoặc nối STT → SLM; checkbox TTS đọc câu trả lời. Microphone cần localhost/HTTPS; khi truy cập Jetson qua HTTP LAN có thể dùng upload audio.
- Mục chi tiết API hiển thị endpoint, HTTP status, thời gian HTTP, request và response/lỗi của từng stage. Token không được ghi vào mục này. Kết quả WAV có audio player.
- Client qua LAN nhập token từ `service.env` rồi bấm **Kết nối**; token chỉ lưu trong tab browser. Browser kết nối loopback đúng hostname `localhost`/`127.0.0.1` được điền token tự động. Model API luôn kiểm tra Bearer token.
- Nút nạp lại chỉ thử STT/TTS chưa sẵn sàng, không tạo thêm model khi chúng đã ready. SLM do process llama-server quản lý; xem log/restart `run` nếu SLM hỏng.

Trang này test inference. Để kiểm thử các nhánh RAG, LangGraph và tool xe của Laptop, dùng CLI pipeline ở phần dưới; các phần đó không chuyển sang Jetson.

## Nhập câu hỏi và test model thật ngay trên máy local

Muốn mở giao diện và giữ cả ba model trên GPU local x86:

```bash
bash scripts/test_ai_ui.sh
```

Mở `http://127.0.0.1:8789/test`. Lần đầu script chuẩn bị các dependency/model; SLM dùng wheel CUDA 12.4 tương thích glibc 2.35+, STT dùng PyTorch CUDA và TTS dùng ONNX Runtime CUDA. Mặc định SLM offload 12 layer để cả ba model cùng nằm trong GPU 4 GB; có thể chọn `--gpu-layers` và `--port`. CUDA không hoạt động sẽ báo lỗi, không tự chuyển sang CPU. Log local UI và token nếu browser yêu cầu nằm ở `data/local-ai-ui/`. Ctrl+C dừng các process do script tạo.

Trên Xavier, chính lệnh `bash scripts/test_ai_ui.sh` chuyển sang setup/run native ở mount dữ liệu; không cài wheel x86. Profile Jetson dùng full SLM offload. Hai lệnh setup/run riêng vẫn là cách vận hành chính ở phần Jetson.

Từ thư mục repo, chạy:

```bash
bash scripts/test_local_ai.sh
```

Script dùng Qwen3-1.7B Q4 thật trên CPU và **chính gateway `jetson_ai`** sẽ dùng ở Jetson. Lần đầu tự tải model/binary đã pin checksum; các lần sau dùng lại. API bind localhost trên port trống, token được tạo riêng cho lần test. Khi hiện `Bạn>`, nhập câu hỏi và nhấn Enter. `/new` xóa hội thoại của client; `/quit` hoặc Ctrl+C dừng cả gateway và model. Model được giữ lại trên ổ đĩa. Log ở `data/local-ai-test/{llama,gateway}.log`.

Muốn thử cả Laptop RAG, LangGraph và tool xe mô phỏng qua model API này:

```bash
bash scripts/test_local_ai.sh --target pipeline
```

Trong chế độ pipeline, có thể nhập câu hỏi handbook, `Đặt nhiệt độ 25 độ`, hoặc `Mở cửa sổ bên tài`. `/approve` và `/deny` xử lý confirmation đang chờ; `/state` xem state xe; `/driving on` và `/driving off` đổi trạng thái lái của simulator. Response in backend, route, status, action và source ID. History của lượt test nằm trong thư mục tạm riêng, được xóa khi thoát. Dùng `--json` để xem đầy đủ response. Các lần chạy này không ghi đè cấu hình developer.

Input một lần hoặc nhiều lượt từ command line:

```bash
bash scripts/test_local_ai.sh --text '2 cộng 3 bằng bao nhiêu?'
bash scripts/test_local_ai.sh --target pipeline --text 'Mở cửa sổ bên tài' --text '/approve'
```

Nếu chỉ muốn kiểm tra core offline bằng rules, dùng `.venv/bin/python scripts/test_pipeline.py`. Nếu đã có Jetson thật đang chạy, dùng `.venv/bin/python scripts/test_pipeline.py --mode jetson --jetson-url http://<ip>:8789` với `JETSON_AI_TOKEN` trong env. CPU local xác nhận được API/pipeline; tốc độ và khả năng build CUDA trên AGX vẫn phải đo trên thiết bị thật. Voice STT/TTS chưa có trong bài test text này.

Kết quả smoke local đã chạy với model thật: prompt tương tác hoạt động, `2 cộng 3` trả `5`, `/quit` dừng cả hai process. Qua pipeline, đặt điều hòa 25 độ và confirmation/approve chạy được. Tuy nhiên Qwen3-1.7B đã chọn sai zone `front_passenger` cho câu “Mở cửa sổ bên tài” và phân loại câu hỏi hướng dẫn sạc thành `unsupported`; rules đối chiếu trả đúng zone `driver` và có retrieval/citation. Đây là lỗi hiểu yêu cầu của model cần xử lý trước nghiệm thu pipeline. Không coi API hoạt động là model đã đạt độ chính xác nghiệp vụ.

## Pull nhánh trên Jetson

Trong checkout repo trên Xavier:

```bash
git fetch origin
git switch --track origin/feat/jetson-ai-pipeline
```

Nếu đã có nhánh local cùng tên, dùng `git switch feat/jetson-ai-pipeline` rồi `git pull --ff-only`. Giữ các thay đổi cấu hình cục bộ của bạn; nhánh không chỉnh `config.toml`.

## Setup và run trên Jetson

Yêu cầu: AGX Xavier aarch64 với L4T R35.x, CUDA toolkit 11.4, cuDNN 8, mount `/mnt/jetson-data` còn ít nhất 15 GiB cho lần build đầu; `uv`, compiler C++, `git`, `curl`, `openssl` và mạng để tải artifact. Script chuẩn bị Python 3.10 và CMake trong môi trường riêng, không thay `.venv` repo hay CUDA hệ thống. Venv, cache, model, source và log nằm tại `/mnt/jetson-data/vivi-ai`.

```bash
bash scripts/jetson_ai.sh setup --profile agx
bash scripts/jetson_ai.sh run
```

`setup` build llama.cpp `b11447`, whisper.cpp `v1.7.6` cho CUDA kiến trúc Xavier `sm_72`, tải speech model theo revision cố định và kiểm tra SHA256 GGUF. TTS dùng ONNX Runtime CUDA 1.16.3 build native cho CUDA 11.4/cuDNN 8, hoặc wheel riêng qua `JETSON_AI_ORT_WHEEL`. Lần build ONNX Runtime đầu có thể mất nhiều thời gian; lần sau dùng lại binary/wheel. Nếu có venv service Python 3.12 từ mốc LLM cũ, script giữ bản đó với tên `venv-before-speech-*` rồi tạo venv 3.10.

**Điểm cần xác minh trên Xavier:** ZeroTTS 0.1.5 khai báo ONNX Runtime >=1.17. Profile CUDA 11.4 thử bản 1.16.3 riêng và chỉ đánh dấu setup hoàn tất khi nạp CUDA và tạo WAV thật thành công. Chưa chạy phép kiểm này trên AGX; không coi runtime này đã được nghiệm thu. Kiểm tra CPU 1.16.3 không chạy được export FP16 do kernel ArgMax; không dùng CPU đó làm fallback cho TTS. Lý do tránh lấy bản ORT mới tùy ý: [hướng dẫn build chính thức](https://onnxruntime.ai/docs/build/eps.html) yêu cầu CUDA >=11.8 cho JetPack 5 từ ORT 1.17. Khi CUDA build/smoke lỗi, setup dừng và báo bước lỗi; không tự chuyển sang CPU.

`run` ưu tiên địa chỉ Tailscale của Jetson, hoặc localhost nếu không có Tailscale. Muốn Laptop kết nối qua LAN, đặt `JETSON_AI_BIND` bằng IP LAN riêng của Jetson trước khi chạy. llama-server và whisper-server chỉ lắng nghe loopback; gateway dùng Bearer token trong `/mnt/jetson-data/vivi-ai/service.env` (quyền 0600). `Ctrl+C` dừng gateway và model process. Muốn đổi mount, dùng `JETSON_AI_DATA_DIR` trong cả hai lệnh. Không cần chạy Docker hay ba lệnh model riêng.

Kiểm tra capability sau `run`:

```bash
curl -H "Authorization: Bearer <token>" http://<jetson-private-ip>:8789/v1/capabilities
```

`/health/live` chỉ xác nhận process gateway còn sống. Đợi `llm`, `stt`, `tts` đều `ready`; STT/TTS nạp ở nền và mỗi capability có lỗi riêng nếu không nạp được. API trả model path, host, PID, device, `using_gpu`, thời gian load và thời gian xử lý gần nhất. SLM/whisper.cpp xác nhận GPU từ log engine, ZeroTTS từ provider thực tế của các session. Khi gọi một stage, gateway dành slot GPU cho stage đó; request cạnh tranh trả `BACKEND_BUSY`.

### Test text từ terminal trên Xavier

Mở terminal khác tại repo, dùng đúng URL mà `run` đã in:

```bash
set -a
source /mnt/jetson-data/vivi-ai/service.env
set +a
/mnt/jetson-data/vivi-ai/venv/bin/python scripts/test_pipeline.py \
  --mode jetson --target llm --jetson-url http://<jetson-private-ip>:8789
```

Nhập câu hỏi tại `Bạn>`. `/quit` thoát client; service trong terminal `run` tiếp tục chạy.

### Test STT và TTS qua API

STT nhận file **WAV mono PCM16 16 kHz, tối đa 60 giây**. TTS nhận text tối đa 500 ký tự và trả WAV mono 48 kHz kèm `X-ViVi-Latency-Ms`. Sau khi nạp token vào env như trên:

```bash
curl --fail-with-body -H "Authorization: Bearer $JETSON_AI_TOKEN" \
  -F 'file=@input.wav;type=audio/wav' \
  http://<jetson-private-ip>:8789/v1/stt/transcriptions

curl --fail-with-body -H "Authorization: Bearer $JETSON_AI_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Xin chào bạn."}' \
  http://<jetson-private-ip>:8789/v1/tts/synthesize -o answer.wav
```

Trên Ubuntu 20.04 với curl cũ chưa hỗ trợ `--fail-with-body`, thay bằng `--fail`. Endpoint `/v1/llm/generate` trả envelope version 1.0; `/v1/chat/completions` tương thích client local hiện tại. STT/TTS hiện là upload/synthesis một lần, chưa streaming và chưa đủ canonical envelope của kiến trúc đích.

## Laptop

Giữ handbook và index trên Laptop. Dùng client `local` OpenAI-compatible đã có, qua biến môi trường của Laptop:

```bash
export LLM_PROVIDER=local
export LOCAL_LLM_MODEL=vivi-qwen3-1.7b
export LOCAL_LLM_BASE_URL=http://<jetson-private-ip>:8789/v1
export LOCAL_LLM_API_KEY='<token trong service.env>'
export JETSON_AI_URL=http://<jetson-private-ip>:8789
export JETSON_AI_TOKEN='<cùng token>'
export JETSON_AI_POLICY=prefer_local
```

Sau đó chạy Laptop bằng entrypoint hiện tại. `JETSON_AI_POLICY` mặc định `disabled`. Với provider yêu cầu là `local`, `prefer_local` chọn Jetson nếu LLM `ready`; nếu không thì chọn cloud provider đã cấu hình, sau cùng là `rules`. `offline_only` chỉ chọn Jetson hoặc `rules`, từ chối khi client chỉ định cloud; Soniox được thay bằng STT fallback local (hoặc tắt), VieNeu bị tắt và không còn trong danh sách voice chọn được. `allow_cloud` hiện có cùng thứ tự chọn fallback như `prefer_local`; chưa có chính sách lọc dữ liệu riêng cho từng provider. Giữ voice provider hiện có ở Laptop: **remote STT/TTS adapter chưa được nối vào public `/api/v1`**. Server đã có endpoint STT/TTS độc lập để test trên Jetson; không tự chuyển luồng voice đang dùng bởi các thành viên khác. `RAG_RETRIEVAL_MODE=sqlite_local` vẫn truy xuất trên Laptop; khi model E5 thiếu hoặc lỗi nạp, retriever dùng FTS5 trên **cùng handbook SQLite**. Index hỏng hoặc handbook mất vẫn là lỗi, không bị che bằng fallback.

Router chỉ kiểm tra Jetson trước khi bắt đầu lượt. Nó chưa thử lại nếu Jetson/cloud mất giữa lượt; không replay graph vì có thể đã có side effect. Khi cả Jetson và Internet đều mất, `offline_only` đảm bảo rules/RAG text; `prefer_local` chưa tự xác nhận cloud mất mạng trước khi chọn cloud. Lệnh `setup` và model thật **chưa được smoke trên AGX**. Smoke GPU x86 local đã nạp cùng lúc ba model, tạo WAV và nhận dạng qua API; đây không xác nhận tương thích ARM/CUDA 11.4. Whisper-base đã nhận sai vài từ tiếng Việt trong mẫu tổng hợp ngắn, cần eval/thay model trước nghiệm thu giọng nói. Chỉ chuyển mặc định sang topology mới sau smoke trên Xavier và các fault test trong [kế hoạch migration](LAPTOP_JETSON_MIGRATION_PLAN.md).
