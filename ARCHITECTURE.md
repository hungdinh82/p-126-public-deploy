# VIVI architecture

Tài liệu kiến trúc chuẩn nằm tại [docs/architecture_diagram.md](docs/architecture_diagram.md).

Các contract triển khai giữa UI, orchestration, handbook và vehicle nằm tại
[docs/contracts.md](docs/contracts.md). Không tạo entry point hoặc response schema thứ hai
ngoài `src.vivi.api.app:app` và contract `/api/v1/turn`.
