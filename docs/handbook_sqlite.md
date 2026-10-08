# Đưa handbook vào SQLite

Mặc định dùng SQLite FTS5 + vector E5 local, runtime chỉ đọc `handbook.sqlite3`.
`handbook-source.sqlite3` giữ corpus nguồn cho ingestion, rebuild và benchmark.
Profile Jetson Nano tối thiểu vẫn dùng FTS5. Xem [RAG local](rag_local.md) cho AGX Xavier.

## Nguồn VinFast đã hỗ trợ

Chỉ crawl tài liệu mà nhóm có quyền sử dụng:

```bash
.venv/bin/python -m src.vivi.cli.crawl_manual \
  --model VF8 --year 2026 --locale vi_vn
.venv/bin/python -m src.vivi.cli.parse_manual \
  --model VF8 --year 2026 --locale vi_vn
.venv/bin/python -m src.vivi.cli.import_handbook_sqlite \
  --model VF8 --year 2026 --locale vi_vn
.venv/bin/python -m src.vivi.cli.prepare_embeddings
.venv/bin/python -m src.vivi.cli.build_index
```

Các bước tạo lần lượt:

```text
data/handbooks/raw/vf8/2026/vi_vn/
data/handbooks/parsed/vf8/2026/vi_vn/chunks.jsonl
data/handbooks/handbook-source.sqlite3
data/handbooks/handbook.sqlite3
```

Import là idempotent: chunk trùng `source_id` được update và chunk cũ cùng scope
vehicle/year/locale bị xóa. FTS index được cập nhật bằng trigger trong cùng transaction. Import chỉ sửa corpus nguồn;
chạy `build_index` để embedding và xuất index runtime mới, rồi restart backend.
Import trực tiếp vào artifact có vector bị chặn để tránh lệch nội dung/embedding.

## Import handbook tùy chỉnh

Chuẩn hóa tài liệu PDF/HTML thành JSON Lines, mỗi dòng theo contract:

```json
{"source_id":"vf8-2026-001","document_id":"vf8-2026","source_url":"manual://vf8/2026/page/12","vehicle_model":"VF8","model_year":2026,"locale":"vi_vn","chapter_id":1,"chapter":"An toàn","section_path":["An toàn","Cảnh báo"],"content_type":"warning","content":"Nội dung đã trích xuất...","checksum":"sha256-cua-content","chunk_index":0}
```

Sau đó:

```bash
.venv/bin/python -m src.vivi.cli.import_handbook_sqlite \
  --chunks /path/to/chunks.jsonl \
  --database data/handbooks/handbook-source.sqlite3
.venv/bin/python -m src.vivi.cli.build_index
```

Một file import chỉ được chứa một scope `vehicle_model/model_year/locale`. Giữ page trong
`source_url` hoặc metadata nguồn khi parser PDF được bổ sung; không phát citation giả.

## Kiểm tra artifact

```bash
sqlite3 data/handbooks/handbook.sqlite3 \
  'select vehicle_model, model_year, locale, count(*) from handbook_chunks group by 1,2,3;'
sqlite3 data/handbooks/handbook.sqlite3 'pragma integrity_check;'
```

Test truy xuất:

```bash
.venv/bin/python -c "from src.vivi.rag.sqlite_store import SQLiteHandbookStore; s=SQLiteHandbookStore('data/handbooks/handbook.sqlite3', read_only=True); print(s.search('áp suất lốp', vehicle_model='VF8', model_year=2026, locale='vi_vn'))"
```

Chép file `.sqlite3` đã đóng kết nối sang Jetson và mount read-only nếu không cần lưu history.
Conversation history nằm ở file `RAG_HISTORY_DB` khác, không trộn với corpus handbook.
