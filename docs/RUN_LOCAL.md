# Chạy ViVi trên máy tính cá nhân

Hướng dẫn chạy app bình thường trên Linux/Ubuntu hoặc Ubuntu trong WSL.
AI server và backend chính chạy trong hai terminal tại cùng checkout project.
Không cần Jetson hoặc Docker. AI server mặc định dùng GPU NVIDIA.

Trong nhánh thử nghiệm `feat/zerotts-int8-setup`, setup PC mặc định tạo ZeroTTS
INT8 và chạy riêng TTS trên CPU. Qwen/Whisper vẫn theo `--device` (mặc định CUDA).
Setup AGX Xavier giữ FP32 GPU mặc định; chọn INT8 rõ bằng `--tts-precision int8`.
Xem [thử nghiệm INT8](#thử-nghiệm-zerotts-int8) ở cuối trang trước khi chọn profile.

## 1. Cấu hình

Trong `config.toml`:

```toml
llm_provider = "local"
local_llm_model = "vivi-qwen3-1.7b"
ai_server_url = "http://127.0.0.1:8789"
ai_server_auth_required = false
jetson_ai_policy = "prefer_local"
```

Không cần đặt `AI_SERVER_TOKEN` trong `.env`. Các biến môi trường và `.env`
ưu tiên hơn `config.toml`; cập nhật hoặc xóa override provider, URL hay xác thực
cũ nếu muốn dùng cấu hình trên.

Nếu AI server ở máy khác, đổi `ai_server_url` thành URL truy cập được và đặt
`ai_server_auth_required = false` trong cấu hình của cả hai máy. Khi không yêu
cầu token, mọi client truy cập được địa chỉ server đều có thể gọi model.

## 2. Chuẩn bị một lần

Mở terminal trong repo. Đường dẫn trên máy hiện tại:

```bash
cd /home/kah/vinai/P-126
bash scripts/ai_server.sh setup
```

Máy hiện tại đã có dependency/model nên có thể bỏ qua bước này.
Máy mới cần `uv` và GPU NVIDIA hoạt động trong Linux/WSL. Setup chuẩn bị môi
trường Python, dependency và tải model; lần đầu cần Internet.
Chỉ chuyển sang bước chạy khi setup hoàn tất thành công.

## 3. Terminal thứ nhất: chạy AI server

```bash
cd /home/kah/vinai/P-126
bash scripts/ai_server.sh run
```

Giữ terminal này chạy. Service nạp SLM Qwen3-1.7B, STT Whisper và TTS ZeroTTS,
phục vụ API tại `http://127.0.0.1:8789`. SLM nạp trước; speech nạp ở nền.
Lệnh `run` không chạy setup hoặc tải model tự động.

Xem trạng thái từng model, thiết bị GPU và thời gian xử lý tại:

```text
http://127.0.0.1:8789/test
```

Trang này theo dõi và gọi trực tiếp model; không chạy workflow/RAG/xe của app.
Log service nằm trong `data/ai-server/`.

Có thể bật checkbox **SLM streaming**, **TTS streaming**, **STT streaming**
riêng cho mỗi model trong trang này. Chi tiết protocol, giới hạn Whisper và
cập nhật dependency Jetson ở [MODEL_STREAMING.md](MODEL_STREAMING.md).

Các tùy chọn khi cần:

```bash
bash scripts/ai_server.sh run --port 8790
bash scripts/ai_server.sh run --device cpu
bash scripts/ai_server.sh run --gpu-layers 12
```

Nếu đổi cổng, cập nhật `ai_server_url` tương ứng trước khi chạy backend.
Muốn cho máy khác kết nối, dùng `--host <IP-LAN>` hoặc `--host 0.0.0.0`;
backend ở máy khác phải dùng IP/hostname thực tế của AI server.

## 4. Terminal thứ hai: chạy app chính

```bash
cd /home/kah/vinai/P-126
.venv/bin/python run.py
```

Mở trên trình duyệt:

```text
http://127.0.0.1:8787
```

Backend chạy workflow `unified-3`, RAG, session/history và vehicle gateway.
AI server thực hiện inference SLM theo URL đã cấu hình. Mặc định cấu hình hiện
tại dùng xe mô phỏng memory; cấu hình MQTT vẫn hoạt động theo module hiện có.

Giọng nói của app chính vẫn dùng adapter STT/TTS trong cấu hình backend.
Remote STT/TTS của AI server chưa được nối vào public API của app chính.
Nếu chỉ muốn nhập bằng chữ và tránh nạp speech ở backend, chạy:

```bash
STT_PROVIDER=off TTS_PROVIDER=off .venv/bin/python run.py
```

Đây vẫn là app chính; hai biến trên chỉ tắt speech cho lần chạy đó.

## 5. Sử dụng và kiểm tra

Trong app, thử:

- `Đặt điều hòa 24 độ` — xem trạng thái xe mô phỏng thay đổi.
- `Kết nối cáp sạc AC thế nào?` — kiểm tra câu trả lời cẩm nang và nguồn.
- `Mở cửa bên tài` — xác nhận khi được yêu cầu; thao tác vẫn chịu kiểm tra an toàn.

Trạng thái backend:

```text
http://127.0.0.1:8787/api/v1/health
```

Kiểm tra model ở gateway chuyển sang `ready` và provider local khả dụng trong
health. Với `prefer_local`, nếu AI server không sẵn sàng, backend có thể dùng
cloud đã cấu hình hoặc rules. Muốn tránh gọi cloud, đặt
`jetson_ai_policy = "offline_only"` rồi restart backend.

## 6. Dừng và chạy lại

Nhấn **Ctrl+C** ở từng terminal. AI server dừng các model process do nó tạo;
backend dừng riêng. Lần tiếp theo chỉ cần chạy lại hai lệnh `run` và `run.py`,
không cần setup lại nếu môi trường/model không thay đổi.

## Thử nghiệm ZeroTTS INT8

### INT8 trên GPU AGX Xavier — TensorRT

Chọn **`int8-gpu`** để dùng GPU. `int8` trong phần CPU bên dưới vẫn là dynamic
quantization trên CPU; hai profile dùng artifact riêng và không thay thế nhau.

```bash
# Trên AGX Xavier, tại checkout đã có code nhánh này:
bash scripts/jetson_system.sh install --tensorrt
bash scripts/ai_server.sh setup --tts-precision int8-gpu
bash scripts/ai_server.sh run --tts-precision int8-gpu
```

SDK dùng TensorRT 8.5.2 của JetPack 5, không cài TensorRT mới nhất từ PyPI hoặc
nâng JetPack. Bước system chỉ cài các package SDK/parser/plugin cùng phiên bản
runtime đã có. Setup build ORT 1.20.1 với `--use_tensorrt` trong thư mục build
riêng và thêm metadata engine để kiểm tra precision. Build ORT lần đầu có thể
mất nhiều giờ trên Xavier. Qwen/Whisper tiếp tục dùng CUDA như profile hiện có.

Setup vá hai chỗ ORT gọi `setHardwareCompatibilityLevel`, API chỉ có từ TensorRT
8.6, để biên dịch với 8.5.2. Tùy chọn hardware compatibility không được bật trên
Xavier; nếu yêu cầu trên 8.5 thì báo lỗi rõ. Khi sửa lỗi build này, chạy lại setup
sẽ dùng các object CUDA đã build trong cùng thư mục, không cần xóa cache build.

TTS GPU INT8 dùng **static QDQ S8S8**: thu đầu vào thật của ba graph khi FP32 sinh
sáu câu tiếng Việt bằng giọng VIVI, hiệu chuẩn MinMax đối xứng và quantize MatMul
có trọng số hằng, per-channel. Codec, sampling và phần còn lại vẫn FP32. Model
lưu ở `zerotts-int8-gpu/`; artifact này khác `MatMulInteger` của bản CPU.
QDQ mang scale hiệu chuẩn ngay trong graph, nên không đưa calibration table
ngoài vào TensorRT. Tham khảo [ORT TensorRT EP](https://onnxruntime.ai/docs/execution-providers/TensorRT-ExecutionProvider.html).

Setup không chọn profile GPU trước khi smoke suy luận thật thành công. Runtime
bắt buộc `--device cuda`, khởi tạo TensorRT EP và từ chối thay toàn bộ session
bằng CUDA/CPU khi TensorRT lỗi. Mỗi graph phải có sự kiện thực thi TensorRT trong
ORT profiling, đồng thời engine inspector phải tìm thấy lớp MatMul/GEMM/FC có
format INT8. Nếu thiếu bằng chứng thì TTS báo `unavailable`, không báo INT8 GPU ready.
Các node không được TensorRT hỗ trợ vẫn có thể chạy CUDA/CPU; báo cáo `provider_profile`
cho thấy số sự kiện của từng provider. Đây là profile hỗn hợp, không phải mọi node
đều INT8 hoặc đều trên GPU.

Cache engine nằm trong `models/speech/trt-cache/`, tách theo UUID GPU, driver,
checksum thư viện TensorRT, ORT, model và lựa chọn precision/workspace. Engine
không được sao chép từ PC sang Xavier. Hình dạng mới có thể khiến TensorRT build
lại engine; cần đo TTFA/RTF với các câu thực tế trên Xavier trước khi dùng thường xuyên.

Đo trên Xavier sau setup:

```bash
source scripts/jetson_profile.sh
export JETSON_AI_TRT_INSPECTOR="$VIVI_DATA/build/trt-inspect"
"$VIVI_VENV/bin/python" scripts/benchmark_zerotts.py --precision int8-gpu --device cuda \
  --root "$VIVI_DATA/models/speech" --output "$VIVI_DATA/logs/tts-int8-gpu"
```

Để tạo QDQ trên PC từ model đã tải, chỉ kiểm tra chuyển đổi, không xác nhận GPU:

```bash
.venv/bin/python scripts/prepare_speech_models.py --root models/ai-speech \
  --tts-precision int8-gpu --local-only --defer-selection
```

Có thể thêm `--calibration-texts <file.txt>` (mỗi dòng một câu) cho dữ liệu thực tế.
Corpus mặc định là smoke ban đầu, chưa phải bộ đánh giá chất lượng giọng đầy đủ.

Đã kiểm tra local: tạo ba graph QDQ với 72/72/425 MatMul INT8, giữ integer I/O,
chạy các feed thật và sinh WAV/streaming VIVI bằng QDQ trên CPU. Model tổng cộng
khoảng 356 MB, gồm codec FP32. Đã build thành công provider CUDA/TensorRT trên
AGX Xavier sau bản vá API TensorRT 8.5.2. **Chưa xác nhận engine INT8 hay tốc độ
GPU trên AGX Xavier**; setup vẫn cần vượt qua smoke suy luận và engine inspector.

### INT8 trên CPU — thử nghiệm trước đó

Theo [hướng dẫn browser của ZeroTTS](https://github.com/zeroweight-ai/ZeroTTS/blob/main/docs/BROWSER.md),
INT8 ONNX chậm hơn FP32 trong WASM. Nhưng
[benchmark native x86](https://github.com/zeroweight-ai/ZeroTTS/blob/main/docs/BENCHMARKS.md)
ghi nhận cải thiện trên một máy Ryzen/DDR4; kết quả đó chưa chứng minh tốc độ trên Xavier.
Nhánh này áp dụng cách chuyển đổi của
[script upstream](https://github.com/zeroweight-ai/ZeroTTS/blob/main/tools/quantize_onnx_int8.py):
ORT BASIC loại bỏ Identity, rồi dynamic-quantize MatMul trong ba graph transformer.
Codec, embedding và các phép tính còn lại vẫn FP32; token/length INT32/INT64 giữ nguyên.
Đây là profile hỗn hợp, không phải toàn bộ model/tensor đều INT8.
Trọng số lưu INT8 với `reduce_range=True` (miền giá trị 7 bit) để tránh bão hòa
tích lũy U8S8 trên CPU x86 AVX2 khi thử local; đây là khác biệt so với mặc định
script upstream. Chất lượng giọng trên Xavier vẫn cần nghe/đo lại.

PC:

```bash
bash scripts/ai_server.sh setup --tts-precision int8
bash scripts/ai_server.sh run --tts-precision int8
```

Nếu đã tải đủ model, chuyển đổi offline mà không cài lại môi trường:

```bash
UV_CACHE_DIR=/tmp/vivi-uv-cache uv pip install --python .venv/bin/python onnx==1.17.0
.venv/bin/python scripts/prepare_speech_models.py \
  --root models/ai-speech --tts-precision int8 --local-only
```

AGX Xavier:

```bash
bash scripts/ai_server.sh setup --tts-precision int8
bash scripts/ai_server.sh run --tts-precision int8
```

Trên Xavier, Qwen/Whisper vẫn chạy CUDA, riêng INT8 TTS dùng CPUExecutionProvider.
Đây là lựa chọn thử để giảm bộ nhớ GPU, không phải TensorRT INT8.
FP16 GPU là hướng thử tiếp theo nếu ưu tiên tốc độ GPU; nhánh này chưa thêm FP16.

Setup giữ model FP32 ở `zerotts/`, tạo bản riêng ở `zerotts-int8/`, ghi phiên bản,
checksum và số node INT8 trong `quantization.json`, rồi mới chọn profile qua
`tts-profile.json`. Chuyển đổi lỗi không đổi profile/model INT8 cũ. Setup lại tái
sử dụng artifact nếu nguồn, công cụ và checksum đầu ra khớp. Runtime không chuyển đổi
hay tải model. Trang `/test` và `/v1/capabilities` báo precision cùng thiết bị TTS.

So sánh bằng hai tiến trình riêng; đổi `--device cpu` thành `cuda` khi đo FP32 GPU:

```bash
.venv/bin/python scripts/benchmark_zerotts.py --precision fp32 --device cpu \
  --output data/zerotts-comparison/fp32
.venv/bin/python scripts/benchmark_zerotts.py --precision int8 --device cpu \
  --output data/zerotts-comparison/int8
```

Trên Xavier dùng Python của profile và đường dẫn model trên mount dữ liệu:

```bash
source scripts/jetson_profile.sh
"$VIVI_VENV/bin/python" scripts/benchmark_zerotts.py --precision fp32 --device cuda \
  --root "$VIVI_DATA/models/speech" --output "$VIVI_DATA/logs/tts-fp32"
"$VIVI_VENV/bin/python" scripts/benchmark_zerotts.py --precision int8 --device cpu \
  --root "$VIVI_DATA/models/speech" --output "$VIVI_DATA/logs/tts-int8"
```

Script dùng giọng VIVI, cùng câu và seed, một warmup và ba lượt đo; lưu WAV và
`report.json` với thời gian audio đầu tiên, tổng thời gian và RTF (nhỏ hơn 1 là
nhanh hơn thời gian thực). Đọc vài câu thực tế và nghe đối chiếu trước khi chọn
INT8; WAV hợp lệ chưa chứng minh giọng nói giữ nguyên chất lượng. Giới hạn thử
là 300 frame, khoảng 24 giây; dùng câu ngắn để tránh bị cắt.

Để chạy lại FP32 ngay, trên cả PC và Xavier:

```bash
bash scripts/ai_server.sh run --tts-precision fp32
```

Để lưu FP32 thành lựa chọn setup mặc định cho máy đó:

```bash
bash scripts/ai_server.sh setup --tts-precision fp32
```

## Kiểm tra INT8 trên máy phát triển

Đã chuyển đủ ba graph: text encoder 72, prefix step 72 và local frame decode
425 node MatMulInteger. Model tổng cộng giảm từ khoảng 904 MB xuống 354 MB,
bao gồm codec FP32. Setup offline chạy lại xác nhận tái sử dụng checksum hợp lệ.
24 test Python cho chuyển đổi, profile, setup và cấu hình đều pass; kiểm tra
frontend streaming, lint Python và cú pháp shell cũng pass.

Câu `Xin chào bạn, tôi là trợ lý ViVi.`, giọng VIVI, hai tiến trình CPU riêng,
một warmup và ba lượt đo: FP32 RTF trung bình 0.94, audio đầu tiên 144 ms;
INT8 RTF 0.41, audio đầu tiên 61 ms. WAV và report local nằm trong
`data/zerotts-int8-smoke/{fp32,int8}/`. Audio hai profile có độ dài khác nhau;
đây là smoke hiệu năng với một câu, chưa đánh giá WER/chất lượng giọng hay tốc độ
trên AGX Xavier. Lượt INT8 yêu cầu `--device cuda` vẫn báo đúng TTS CPU, để kiểm
tra việc tách thiết bị TTS khỏi SLM/STT.

## Kiểm tra FP32 GPU trước đây

Entry point `ai_server.sh run` đã được kiểm tra trên RTX 3050 Ti Laptop GPU:
SLM/STT/TTS đều báo `ready` và sử dụng GPU; gọi chat API không token trả HTTP 200
và SLM trả lời đúng câu `2 cộng 3 bằng bao nhiêu?`. Service đã dừng sau kiểm tra.
Kết quả này xác nhận service khởi động và chat API hoạt động; chưa xác nhận
độ chính xác mọi tác vụ hay chất lượng STT/TTS qua suy luận thực tế ở lần này.
