# Đánh giá Core v2

Contract runtime và migration: [langgraph_core_v2.md](../docs/langgraph_core_v2.md).

```bash
make check
.venv/bin/python -m eval.workflow_benchmark --repeats 3
.venv/bin/python -m eval.navigation_controls_benchmark --samples 100
# Chủ động gọi model thật, không thực thi capability:
.venv/bin/python -m eval.semantic_benchmark --provider google
```

| Bộ | Dependency | Đo gì |
|---|---|---|
| `workflow_benchmark` | Semantic viết tay độc lập; map/xe tổng hợp; LangGraph, SQLite, orchestrator thật | Task, consent, slot, draft/journey, fallback, replay/collision, stream, restart |
| `navigation_controls_benchmark` | Journey được xác nhận; model/map bị giữ bằng barrier | Backend P/D khi bình thường/model chờ/map chờ |
| `semantic_benchmark` | Model thật do người chạy chọn; không có map/xe | Capability/slot trên dataset ngôn ngữ riêng; fallback không tính đúng |
| `benchmark` | Cẩm nang và embedding/model local tùy chọn | Retrieval/answer development benchmark riêng |

Workflow dùng [core_cases.jsonl](core_cases.jsonl). Mỗi operation có ID, input/semantic,
oracle `expect` độc lập. Không gọi parser để tạo model stub hoặc cập nhật expected từ
output. `$ref` chỉ lấy ID/revision/binding hoặc giá trị từ bước trước. Runner kiểm
vehicle state thực tế, receipts và domain; lỗi ghi vào report và exit 1.

Semantic dataset ở [semantic_cases.jsonl](semantic_cases.jsonl) không dùng làm model
stub. Bộ này được viết trong quá trình phát triển, chưa phải held-out đã duyệt;
không được công bố là độ chính xác tổng quát. Semantic runner chỉ gọi provider
được chọn, ghi timeout/repair và đếm fallback riêng. Không tự gọi live eval trong
`make check` hoặc CI.

P/D có tối thiểu 100 mẫu **mỗi số** cho từng tải (600 mẫu tổng), warmup riêng.
Chia batch 10 mẫu mỗi số, khởi tạo journey và dependency đang chờ trước mỗi batch;
bảo đảm mọi sample được đo trong deadline thật của model/map. Không tăng timeout
để giữ dependency giả. Pass khi p95 của **từng số** ≤300 ms bình thường, ≤500 ms
khi model/map chờ. Phạm vi là orchestrator, navigation domain, SQLite và durable
receipt; chưa gồm HTTP/browser, mạng xe hay phần cứng Jetson.

Report mặc định: `eval/results/core-v2.json`, `controls-v2.json`, `semantic-v2.json`.
Có timestamp, git revision/dirty, dataset hoặc fixture hash, môi trường và hạn chế.
Các runner offline dùng DB tạm và chặn mạng ngoài loopback; không đọc/ghi DB người dùng.
Có thể dùng `--output /tmp/<name>.json` để không lưu artifact vào repo.

Tests theo trách nhiệm ở `tests/{unit,contract,integration,frontend}`. Model/map/MQTT
thật và phần cứng là phần nghiệm thu riêng. Các scenario/runner v1 được giữ ở
[legacy](legacy/README.md), không tính vào kết quả Core v2. Những artifact cũ trong
`results/` và `artifacts/` có revision riêng, không chứng minh revision hiện hành pass.

Các report live đã chạy nằm ở `results/semantic-holdout-v2.json`,
`semantic-holdout-after-fix-v2.json` và `semantic-dependencies-v2.json`. Hai bộ đầu
được dùng phát triển sau khi thấy lỗi; giữ nguyên điểm và prompt/hash tại thời
điểm đo. Bộ dependencies đo sau cùng đạt 5/6, còn nhầm purpose ở `deps-new-5`.
Không gộp điểm giữa các phiên bản prompt hoặc tính lại bộ đã dùng sửa như held-out.
[Evidence và giới hạn nghiệm thu](../docs/core_v2_acceptance.md) ghi rõ phạm vi.

Bộ `local_semantic_context_cases.jsonl` kiểm local interpreter trên cùng câu nói ở
phiên trống và phiên có draft/journey tổng hợp. Có oracle cho số action, slot và
`blocked` (clarify/unsupported hoặc cờ chặn toàn bộ compound); không có hiệu ứng xe.
Runner nhận `session`/`history` từ từng fixture và `--repeats N`, dùng budget cấu hình
production. Corpus này đã được dùng phát triển, không phải held-out. Bằng chứng và
các lỗi chưa đạt: [Jetson runtime 17/10](../docs/investigations/local-model-runtime-2026-10-17.md).
