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

## Thực hiện trên Jetson: cài một lần, setup và run

Các thay đổi này chưa commit/push. Không dùng `git pull` để lấy bản mới cho đến khi bạn quyết định đẩy nhánh. Có thể chuyển riêng các file bằng gói `data/jetson-ai-profile.tar.gz` được tạo ở máy phát triển; gói không chứa token, model hay cấu hình cá nhân.

Từ **PowerShell trên máy đã có SSH alias `jetson`**, chạy (đổi tên distro/path nếu checkout của bạn ở nơi khác):

```powershell
scp "\\wsl.localhost\Ubuntu-22.04\home\kah\vinai\P-126\data\jetson-ai-profile.tar.gz" jetson:/tmp/
ssh jetson
```

Sau đó trong **terminal Jetson**:

```bash
cd ~/vinai/P-126
# Kiểm tra các đường dẫn trong gói trước khi giải nén.
tar -tzf /tmp/jetson-ai-profile.tar.gz
# Sao lưu các file tương ứng hiện có để có thể phục hồi launcher cũ.
tar -tzf /tmp/jetson-ai-profile.tar.gz | while IFS= read -r path; do
  [ ! -f "$path" ] || printf '%s\n' "$path"
done > /tmp/vivi-existing-files.txt
tar -czf /tmp/vivi-before-profile.tar.gz -T /tmp/vivi-existing-files.txt
tar -xzf /tmp/jetson-ai-profile.tar.gz

bash scripts/jetson_system.sh install
bash scripts/jetson_ai.sh setup --profile agx
bash scripts/jetson_ai.sh run
```

Chỉ chạy lệnh tiếp theo khi lệnh trước thành công. `install` cần nhập mật khẩu sudo của user Jetson trong terminal; phiên SSH tự động không có mật khẩu nên chưa thực hiện phần này.

| Chạy ở đâu / khi nào | Script làm gì |
| --- | --- |
| SSH Jetson, một lần: `jetson_system.sh install` | Thêm repository CUDA arm64 chính thức, kiểm tra fingerprint, mô phỏng APT và dung lượng; cài GCC/G++ 10.5, các package CUDA 11.8/compat được pin và thư viện hệ thống. Giữ cuDNN 8.6 có sẵn, OS/JetPack và lựa chọn CUDA mặc định. |
| Jetson, user thường: `jetson_ai.sh setup --profile agx` | Tạo Python 3.12.15 + venv mới; cài dependency lock có hash; build llama.cpp, whisper.cpp và ORT 1.20.1 GPU; tải model gốc; kiểm tra CUDA và chạy suy luận thật cả ba engine cùng nạp. Thành công mới tạo token/manifest/marker. |
| Jetson, mỗi lần bật service: `jetson_ai.sh run` | Kiểm tra profile/manifest và khởi động STT, SLM, TTS cùng gateway/giao diện `/test`. Không tải model hay nâng dependency. |

Venv mới: `/mnt/jetson-data/vivi-ai/envs/agx-cu118-ort1201-py312/venv`. `.venv` repo và venv AI 3.10 cũ vẫn được giữ. Thư mục build CUDA mới là `build-cu118-gcc10`; model/cache trên mount dữ liệu được dùng lại khi phù hợp.

Lần đầu setup phải biên dịch native, có thể lâu hàng giờ tùy máy. Chạy lại dùng cache; lỗi bước nào thì xem thông báo bước đó, không tạo token thủ công để bỏ qua smoke. Nếu APT báo thiếu root space, dừng và giải phóng dung lượng trước; không chuyển sang cài trong root khi ổ dữ liệu chưa mount.

Sau reboot hoặc khi cần kiểm tra:

```bash
bash scripts/jetson_system.sh check
bash scripts/jetson_ai.sh doctor
```

`check` chỉ đọc các prerequisite hệ thống, `doctor` kiểm tra venv/runtime và CUDA thực tế; không tự sửa môi trường. `uv` đã có ở `$HOME/.local/bin` trên Jetson hiện tại. Trên máy khác thiếu `uv`, cần cài trước khi chạy setup. Mở URL `/test` mà `run` in ra, đợi ba model `ready`, rồi thử text/audio. Smoke setup xác nhận engine chạy được; độ chính xác tiếng Việt và latency mục tiêu cần đánh giá riêng.

### Nếu bản installer trước báo thiếu `cusparse.h` khi build ORT

Installer trước đã thiếu `libcusparse-dev-11-8`. Bản hiện tại bổ sung package pin `11.7.5.86-1` và kiểm tra các header CUDA trước khi build. Trên máy đã chạy bản cũ, cài bổ sung rồi tiếp tục setup, không xóa build/cache:

```bash
sudo apt-get -o Dir::Cache::archives=/mnt/jetson-data/vivi-ai/cache/apt \
  install --no-install-recommends libcusparse-dev-11-8=11.7.5.86-1
test -r /usr/local/cuda-11.8/include/cusparse.h && echo 'cuSPARSE header OK'
bash scripts/jetson_ai.sh setup --profile agx
```

APT sẽ cài cả thư viện runtime cuSPARSE phụ thuộc và báo dung lượng cần thêm trước khi xác nhận. Đây là bổ sung thư viện cho CUDA 11.8, không thay Python/model.

## Môi trường mục tiêu trên AGX Xavier

**Quyết định ngày 09/10/2026:** giữ nguyên OS và JetPack; ưu tiên Python 3.12; giữ graph ZeroTTS nguyên bản. Không dùng bản vá ArgMax. Đây là bộ mục tiêu đã kiểm tra compatibility và một phần bằng model thật, chưa được nghiệm thu CUDA trên Xavier. Script trong workspace đã chuyển sang profile `agx-cu118-ort1201-py312`; phần cài hệ thống nằm riêng trong `jetson_system.sh`. Chưa cài bộ mới và chưa nghiệm thu GPU trên Jetson.

### Snapshot SSH, không thay đổi máy

SSH qua alias `jetson` trên Windows đã xác nhận:

- Ubuntu 20.04.6, L4T R35.6.5 (`nvidia-l4t-core` 35.6.5-20260701022834); giữ nguyên OS/kernel/BSP và source `r35.6`.
- CUDA 11.4.315, cuDNN runtime + dev 8.6.0.166-1+cuda11.4, GCC 9.4.
- Python hệ thống 3.8.10 và 3.9.5; Python managed 3.12.15 ở `/home/p126/.local/share/uv/python/cpython-3.12.15-linux-aarch64-gnu/bin/python3.12` đang được `.venv` repo dùng. Service AI cũ dùng 3.10.22 ở `/mnt/jetson-data/vivi-ai/venv`. `python3.10`/`python3.12` không có trực tiếp trong PATH; điều đó không có nghĩa các interpreter chưa tồn tại.
- Root còn 3.5 GiB; mount `/mnt/jetson-data` trên `/dev/mmcblk1p1` còn 46 GiB. GCC 10 chưa cài nhưng APT có candidate 10.5.0-1ubuntu1~20.04. Repository hiện chưa có candidate CUDA 11.8.

### Bộ phiên bản chọn

| Thành phần | Phiên bản / cách sử dụng |
| --- | --- |
| OS / BSP | Ubuntu 20.04.6 / L4T R35.6.5 hiện có; không flash hay dist-upgrade |
| CUDA cho AI | CUDA 11.8.0, nvcc 11.8.89, cài toolkit + `cuda-compat-11-8` chính thức song song CUDA 11.4 |
| cuDNN | Giữ runtime + headers 8.6.0.166 của JetPack |
| Python AI | CPython 3.12.15 managed, venv riêng dưới mount dữ liệu |
| Compiler | GCC/G++ 10.5.0, chọn bằng CC/CXX/CMAKE_CUDA_HOST_COMPILER riêng cho build |
| CMake | 3.28.4 trong môi trường build riêng |
| NumPy | 1.26.4 |
| ONNX Runtime | 1.20.1 GPU, wheel cp312/aarch64 build với CUDA 11.8, cuDNN 8.6, sm_72 |
| TTS | ZeroTTS 0.1.5, model revision `c2bfbd67dc648cac455077333f7cf5c18a2e3bb4`, VIVI; graph nguyên bản |
| STT | whisper.cpp v1.7.6 + Whisper-base, CUDA sm_72; không thêm PyTorch vào service Xavier |
| SLM | llama.cpp b11447 + Qwen3-1.7B Q4_K_M đang pin; build cho CUDA 11.8/sm_72 |

[Cơ chế NVIDIA upgrade CUDA trên JetPack 5](https://developer.nvidia.com/blog/simplifying-cuda-upgrades-for-nvidia-jetson-users/) giữ nguyên Jetson Linux BSP. [Hướng dẫn build ORT](https://onnxruntime.ai/docs/build/eps.html#nvidia-jetson-tx1tx2nanoxavierorin) yêu cầu CUDA >=11.8 và GCC >9.4 cho ORT >=1.17 trên JetPack 5. [cuDNN 8.6 support matrix](https://docs.nvidia.com/deeplearning/cudnn/archives/cudnn-860/support-matrix/index.html) hỗ trợ CUDA 11.8 và cuDNN CUDA 11.x có dynamic-link compatibility trong họ 11.x.

Chọn native + môi trường AI riêng để phù hợp launcher đang có và tránh thêm lớp Docker trong lần chuyển đổi này. Để các máy sau setup đơn giản, build/kiểm thử wheel và native binary một lần trên Xavier rồi đóng gói artifact có SHA256; setup bình thường tải và xác minh artifact, không phải biên dịch lại mỗi lần.

### Kiểm tra đã làm và giới hạn

- Trong venv tạm local Python **3.12.13**, NumPy 1.26.4, ORT **1.20.1 CPU**, cả 5 graph model gốc nạp thành công, ZeroTTS + VIVI tạo WAV thật. Phép thử Python 3.10 cũng thành công. Interpreter mục tiêu trên Jetson là 3.12.15; chưa chạy phép thử GPU này trên ARM.
- [Bảng kernel ORT 1.20.1](https://github.com/microsoft/onnxruntime/blob/v1.20.1/docs/OperatorKernels.md) hỗ trợ ArgMax(13) INT64 trên CPU, CUDA ArgMax vẫn giới hạn opset 1–11. Cho phép partition vài node sang CPU trong session CUDA là hoạt động bình thường; không mô tả TTS là mọi node đều chạy GPU. Nghiệm thu phải kiểm tra profiling và latency, không chỉ `get_available_providers()`.
- ORT 1.16.3 đã build trên Xavier nhưng model gốc không nạp được vì ArgMax(13) INT64; loại bộ đó khỏi đường setup mục tiêu. Đổi Python riêng lẻ không giải quyết lỗi kernel này.
- Chưa cài CUDA 11.8/GCC 10, chưa build wheel GPU 1.20.1, chưa sửa OS/JetPack trên Jetson.

### Các kiểm tra trong đường setup mới

1. Preflight kiểm tra AGX/aarch64/sm_72, L4T/Ubuntu, mount + free space, phiên bản thực tế, quyền cài package. Báo toàn bộ thiếu sót trước khi tải/build nặng. Dùng filesystem UUID để ổ dữ liệu tự mount sau reboot.
2. Cài compiler và toolkit NVIDIA đúng version/package architecture **aarch64-Jetson**, kiểm tra chữ ký package. Không dùng package SBSA/x86, meta-package `cuda` không pin, `dist-upgrade` hay đổi Python hệ thống. Tính dung lượng theo simulation APT trước khi cài; đặt cache APT trên mount dữ liệu vì root chỉ còn 3.5 GiB.
3. Giữ CUDA 11.4 và lựa chọn `/usr/local/cuda` của các module khác. AI chọn rõ `/usr/local/cuda-11.8`, compat driver, CC/CXX; chỉ thiết lập loader/PATH cho process AI, không ghi đè cấu hình shell toàn máy.
4. Tạo venv **mới** cho Python 3.12.15 + ORT 1.20.1, giữ nguyên `.venv` repo và venv AI 3.10 cũ để rollback. Không cài đồng thời hai distribution `onnxruntime` và `onnxruntime-gpu` vào cùng namespace. Cài đủ dependency ZeroTTS; `--no-deps` nếu dùng để tránh CPU wheel phải có kiểm tra dependency riêng, không dùng để bỏ qua yêu cầu phiên bản.
5. Lock dependency Python trực tiếp và gián tiếp theo Python 3.12/aarch64. Pin source commit/revision, package version, wheel/binary/model SHA256. ORT 1.20.1 vẫn có Eigen ZIP checksum cũ trong deps.txt: xử lý metadata checksum đã xác minh trước build, giữ kiểm tra integrity, không vá source toán tử/model. ZIP SHA1 hiện tại `32b145f525a8308d7ab1c09388b2e288312d8eba` đã được đối chiếu 1.784 file với đúng Eigen commit `e7248b26a1ed53fa030c5c459f7ea095dfd276ac`, không thiếu/thừa/khác nội dung.
6. Build trong thư mục riêng cho profile mới; không dùng nhầm CMake cache/binary CUDA 11.4. Tắt build unit-test executable ORT trong bộ deploy bằng `onnxruntime_BUILD_UNIT_TESTS=OFF`; vẫn bắt buộc smoke runtime/API. Build artifact một lần rồi xuất gói đã kiểm chứng để setup máy khác không phải build lại.
7. Tải model gốc theo revision/hash; nếu từng chạy patch thì phục hồi artifact gốc trước smoke. Không sửa graph ArgMax, không tự đổi model/fallback CPU cả engine khi lỗi.
8. Smoke actual CUDA driver/runtime, SLM generate, STT WAV, TTS tạo WAV với model nguyên bản, sau đó chạy chung ba engine để kiểm tra RAM/GPU và gọi API. Ghi provider/node placement thật, latency, phiên bản vào manifest; thiếu CUDA hoặc lỗi bất kỳ engine nào thì setup chưa hoàn tất.
9. Chỉ ghi marker/profile manifest và tạo token khi mọi check đã qua. `run` đọc manifest và từ chối profile lệch version; lỗi setup phải chỉ rõ bước lỗi thay vì chỉ báo missing token. Khi chạy không tự tải hay nâng package. Cung cấp `doctor` đọc-only để kiểm tra môi trường sau reboot.

Đường setup mới thực hiện preflight, lock/hash, build và smoke chung ba engine trước khi ghi marker hoàn tất. Phân phối binary/wheel đã nghiệm thu cho máy khác và profiling placement từng node TTS vẫn là công việc tiếp theo; hiện lần đầu setup sẽ build native trên Xavier. Kết quả inference/latency nằm trong `logs/setup-smoke.json`, phiên bản và checksum trong manifest profile.

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
/mnt/jetson-data/vivi-ai/envs/agx-cu118-ort1201-py312/venv/bin/python scripts/test_pipeline.py \
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

Router chỉ kiểm tra Jetson trước khi bắt đầu lượt. Nó chưa thử lại nếu Jetson/cloud mất giữa lượt; không replay graph vì có thể đã có side effect. Khi cả Jetson và Internet đều mất, `offline_only` đảm bảo rules/RAG text; `prefer_local` chưa tự xác nhận cloud mất mạng trước khi chọn cloud. Lệnh `setup` và model thật **chưa được smoke trên AGX**. Smoke GPU x86 local đã nạp cùng lúc ba model, tạo WAV và nhận dạng qua API; đây không xác nhận tương thích ARM/CUDA 11.8 của profile mới. Whisper-base đã nhận sai vài từ tiếng Việt trong mẫu tổng hợp ngắn, cần eval/thay model trước nghiệm thu giọng nói. Chỉ chuyển mặc định sang topology mới sau smoke trên Xavier và các fault test trong [kế hoạch migration](LAPTOP_JETSON_MIGRATION_PLAN.md).
