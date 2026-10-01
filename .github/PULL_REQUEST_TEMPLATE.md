## Thay đổi gì

<!-- Mô tả ngắn gọn. Nếu sửa Technical Book, ghi rõ chương nào và vì sao. -->

## Loại thay đổi

- [ ] `fix` — sửa lỗi
- [ ] `feat` — tính năng mới
- [ ] `docs` — **nội dung Technical Book** (`docs/guide/**`) → cần review của @AI20K-Build-Phase/book-maintainers
- [ ] `chore` / `refactor` / `test`

## Checklist

- [ ] `bash scripts/check_local.sh` xanh
- [ ] Nếu thay đổi runtime/container: `bash scripts/check_docker.sh` xanh
- [ ] **Không có API key / token / credential** trong diff (kể cả `.env.example`)
- [ ] Nếu đụng `docs/guide/**`: đã kiểm tra nội dung hiển thị đúng và link không chết

**Kết quả kiểm tra local:**

<!-- Dán dòng tổng kết, ví dụ: 91 passed, 15 subtests passed. -->

## AI usage

- [ ] Không sử dụng AI cho thay đổi này
- [ ] Có sử dụng AI và log đã được hook/manual logger ghi nhận

**Công cụ/model:**

**AI hỗ trợ phần nào:**

**Thành viên đã kiểm chứng/chỉnh sửa đầu ra bằng cách nào:**

**Test, tài liệu hoặc evidence liên quan:**

<!--
Nhắc: nội dung docs/guide/ merge vào main sẽ được đồng bộ lên
https://phoenix.note.transformerlabs.ai/technical-book
-->
