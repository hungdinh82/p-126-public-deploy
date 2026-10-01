# Cấu trúc repo và hướng dẫn phát triển ViVi

Tài liệu này là bản đồ kỹ thuật cho runtime hiện tại. Mục tiêu của cấu trúc là giữ một
application package duy nhất, dependency đi một chiều và mỗi loại tính năng có một vị trí
rõ ràng.

## 1. Nguyên tắc kiến trúc

Runtime công khai duy nhất là `src.vivi.api.app:app`. Không tạo thêm FastAPI app hoặc
entrypoint song song.

Dependency chính:

```text
API routes
    ↓
Runtime composition → LangGraph orchestration
                         ↓
             agents / domain policies
                  ↓             ↓
             RAG/history    vehicle gateway
                                  ↓
                         memory hoặc MQTT adapter
```

Quy tắc import:

- `domain/` không import API, provider SDK hoặc persistence.
- `agents/` làm việc với contracts và services, không biết FastAPI.
- `api/routes/` chỉ validate HTTP, chọn provider và gọi runtime service.
- `api/runtime.py` là composition root duy nhất được phép khởi tạo adapter cụ thể.
- Vehicle action luôn đi qua `VehicleActionGateway`; không gọi adapter trực tiếp từ
  classifier, graph node hoặc route.
- Secret và lựa chọn provider chỉ đọc qua `Settings`.

## 2. Cây thư mục

```text
P-126/
├── src/vivi/                    # Application package duy nhất
│   ├── api/
│   │   ├── app.py               # Tạo FastAPI app, middleware, lifespan
│   │   ├── runtime.py           # Khởi tạo config, adapters và orchestrator
│   │   └── routes/
│   │       ├── health.py        # Health và provider availability
│   │       ├── turns.py         # Text turn, stream, confirmation
│   │       ├── speech.py        # STT và TTS endpoints
│   │       ├── vehicle.py       # Vehicle state và demo fixtures
│   │       └── web.py           # Static HTML/JS/CSS
│   ├── agents/
│   │   ├── contracts.py         # Structured intent/action/output contracts
│   │   ├── state.py             # Typed LangGraph state
│   │   ├── classifier.py        # Rules, Google, OpenAI-compatible classifiers
│   │   └── graph.py             # Nodes, edges và graph compilation
│   ├── domain/
│   │   ├── models.py            # API/domain request-response models
│   │   ├── safety.py            # Risk policy, limits, confirmation policy
│   │   └── confirmations.py     # One-use confirmation store
│   ├── vehicle/
│   │   ├── gateway.py           # Trust boundary, execute và verify
│   │   ├── memory.py            # In-process adapter cho dev/test
│   │   └── mqtt.py              # Adapter tới simulator độc lập
│   ├── speech/
│   │   ├── stt.py               # STT factory, off/PhoWhisper adapters
│   │   ├── whisper_cpp.py       # Low-memory STT adapter
│   │   └── tts.py               # TTS factory, off/ZeroTTS adapters
│   ├── rag/                      # Retrieval, generation, grounding, vector stores
│   ├── ingestion/                # Crawl, parse và index handbook
│   ├── history/                  # Conversation history SQLite
│   ├── persistence/              # Event/audio storage
│   ├── cli/                      # Ingestion và RAG development commands
│   ├── config.py                 # Toàn bộ environment settings
│   ├── providers.py              # Provider catalog và availability config
│   ├── orchestration.py          # LangGraph → public TurnResponse facade
│   └── structured_llm.py         # OpenAI-compatible structured JSON client
├── vehicle_simulator/            # Service xe độc lập; không thuộc FastAPI ViVi
├── tests/                        # Unit, API, RAG, confirmation và MQTT tests
├── data/                         # Runtime artifacts; phần lớn không commit
├── docs/                         # Product, architecture và setup docs
├── scripts/                      # Setup, smoke test và development utilities
├── app.js / index.html / style.css
├── Dockerfile* / docker-compose.yml
└── Makefile
```

`vehicle_simulator/` được giữ ngoài `src/vivi/` vì có lifecycle, database và MQTT service
riêng. Nó có thể chạy độc lập với API ViVi.

## 3. Setup development

### Baseline text-only

Yêu cầu Python 3.11 hoặc 3.12. Node.js chỉ cần cho kiểm tra syntax frontend.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Dependency chỉ còn ba profile có mục đích khác nhau:

- `requirements.txt`: runtime nhẹ cho API/Docker/Nano.
- `requirements-dev.txt`: runtime + test/lint/handbook ingestion.
- `requirements-ai.txt`: phần AI nặng tùy chọn cho PC; cài thêm sau dev profile khi cần
  PhoWhisper, ZeroTTS hoặc cloud SDK.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
# Tùy chọn trên PC:
.venv/bin/python -m pip install -r requirements-ai.txt
```

Repo chỉ có một `.env.example`. Các giá trị mặc định là text-only; preset PC AI và Nano
được ghi ở cuối file và trong tài liệu setup tương ứng.

Cấu hình nhẹ để dev text:

```dotenv
LLM_PROVIDER=rules
STT_PROVIDER=off
TTS_PROVIDER=off
PHOWHISPER_PRELOAD=false
ZEROTTS_PRELOAD=false
VIVI_VEHICLE_PROVIDER=memory
RAG_RETRIEVAL_MODE=sqlite
```

Khởi động:

```bash
make run
# hoặc
.venv/bin/python -m uvicorn src.vivi.api.app:app --reload --host 127.0.0.1 --port 8787
```

Mở `http://127.0.0.1:8787`. Kiểm tra runtime tại
`http://127.0.0.1:8787/api/v1/health`.

### Bật từng thành phần

- STT local: `STT_PROVIDER=phowhisper` hoặc `whisper_cpp`.
- TTS local: `TTS_PROVIDER=zerotts`.
- LLM local: đặt `LLM_PROVIDER=local`, `LOCAL_LLM_BASE_URL` và
  `LOCAL_LLM_MODEL`.
- Google/OpenAI: đặt provider và API key tương ứng.
- MQTT vehicle: đặt `VIVI_VEHICLE_PROVIDER=mqtt`, chạy broker và
  `vehicle_simulator` theo `docs/VEHICLE_SIMULATOR_SPEC.md`.

Sau khi đổi `.env`, khởi động lại backend vì runtime được compose một lần khi process
khởi động.

## 4. Quy trình development

Trước khi sửa, xác định boundary sở hữu tính năng. Không đặt business rule trong route
hoặc frontend.

```bash
make lint
make test
```

Lệnh tương đương CI:

```bash
.venv/bin/python -m ruff check src vehicle_simulator tests
.venv/bin/python -m pytest tests/ -q
node --check app.js
```

Test cần dùng temporary directory cho SQLite/audio. Không gọi provider online trong unit
test; dùng rules classifier, fake graph hoặc `httpx.MockTransport`.

Log hội thoại được ghi bởi `EventStore` vào
`VIVI_DATA_DIR/YYYY-MM-DD/turns.jsonl` khi `VIVI_STORE_TRANSCRIPTS=true`. Audio chỉ được
giữ khi `VIVI_STORE_AUDIO=true`. Conversation history của graph nằm ở `RAG_HISTORY_DB` và
được phân vùng bằng `session_id`.

## 5. Thêm API endpoint

1. Chọn hoặc tạo file trong `src/vivi/api/routes/` theo capability.
2. Route chỉ parse HTTP contract và gọi object trong `api.runtime.runtime`.
3. Nếu thêm router mới, include nó trong `create_app()`.
4. Đặt request/response contract dùng chung trong `domain/models.py`.
5. Thêm API test trong `tests/test_api/`.

Không khởi tạo model, database hay adapter ở route module.

## 6. Thêm vehicle action

Vehicle action là thay đổi xuyên nhiều boundary; hoàn tất đủ checklist sau:

1. Thêm intent và arguments vào `agents/contracts.py`.
2. Đồng bộ public action model trong `domain/models.py`.
3. Cập nhật classifier instruction và rules classifier trong `agents/classifier.py`.
4. Validate arguments trong node `validate_action` của `agents/graph.py`.
5. Gán risk class và policy trong `domain/safety.py`.
6. Thêm verify rule trong `vehicle/gateway.py`.
7. Implement action ở `vehicle/memory.py`.
8. Nếu dùng MQTT, cập nhật `vehicle_simulator/models.py`, engine và MQTT contract.
9. Thêm test classification, safety, execution, idempotency và confirmation nếu là R2.

Không cho model hoặc browser tự tuyên bố action thành công. Chỉ response sau gateway
execution và state verification mới được dùng từ “đã”.

## 7. Thêm LLM provider

1. Mở rộng `Settings.llm_provider` và thêm settings/key/model cần thiết.
2. Implement `IntentClassifier` trong `agents/classifier.py`.
3. Implement `HandbookGenerator` trong `rag/generator.py`.
4. Đăng ký classifier/generator trong `rag/runtime.py:create_services`.
5. Thêm provider vào catalog và điều kiện cấu hình trong `providers.py`.
6. Thêm biến mẫu và ghi chú profile vào `.env.example`.
7. Test structured output bằng mock transport; test provider xuất hiện trong health.

Classifier phải trả đúng `IntentDecision`; handbook generator phải trả grounded answer và
citations. Provider không được bỏ qua safety graph.

## 8. Thêm STT hoặc TTS provider

STT adapter phải có:

- `name`, `device`, `dtype`;
- `availability() -> tuple[bool, str]`;
- `preload()`;
- `transcribe(path)`.

Đăng ký adapter trong `speech/stt.py:create_stt` và mở rộng `Settings.stt_provider`.

TTS adapter phải có:

- `name`, `device`, `execution_providers`;
- `availability()` và `preload()`;
- `synthesize(text)`;
- `stream(text)` nếu hỗ trợ PCM streaming.

Đăng ký trong `speech/tts.py:create_tts` và mở rộng `Settings.tts_provider`. Khi provider
lỗi, giữ text response và trả lỗi availability rõ ràng; không âm thầm đổi giọng.

## 9. Thêm hoặc thay retrieval/RAG

- Retriever implement `RetrieverPort` trong `rag/runtime.py`.
- Schema chunk/citation nằm ở `rag/schemas.py`.
- Thêm retrieval mode vào `Settings.rag_retrieval_mode` và `create_services()`.
- Mọi handbook response phải đi qua evidence gate và citation validation.
- Ingestion command nằm trong `src.vivi.cli`; không nhúng crawl/index vào API startup.

SQLite FTS5 là baseline local. Hybrid/Chroma là tùy chọn, không được làm baseline rules
phụ thuộc API embedding.

## 10. Khi nào nên tách thêm file

`agents/graph.py` hiện là hotspot lớn nhất. Các node cùng chia sẻ một `HandbookServices`
runtime và routing contract, nên chưa tách cơ học trong lần migration này. Khi bổ sung một
nhánh nghiệp vụ đáng kể, hãy tách theo capability thành `agents/nodes/handbook.py`,
`agents/nodes/actions.py`, `agents/nodes/conversation.py`; giữ `graph.py` chỉ để đăng ký node
và edge. Không tạo một file cho mỗi hàm nhỏ nếu chúng luôn thay đổi cùng nhau.

Một module nên được tách khi có ít nhất một trong các dấu hiệu:

- có dependency riêng không cần cho phần còn lại;
- có thể test độc lập qua contract rõ ràng;
- hai nhóm thay đổi bởi hai feature khác nhau;
- file trở thành điểm conflict thường xuyên.

## 11. Definition of done

Một feature hoàn tất khi:

- không tạo source root hoặc entrypoint thứ hai;
- config có default an toàn và có trong `.env.example` nếu cần;
- output model được validate;
- vehicle action qua safety và verify;
- provider lỗi có health detail/fallback rõ ràng;
- unit/integration test tương ứng đã có;
- Ruff, pytest và frontend syntax check đều qua;
- tài liệu này hoặc tài liệu capability được cập nhật nếu cấu trúc thay đổi.
