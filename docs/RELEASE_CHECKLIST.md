# Checklist phát hành ViVi

Dùng checklist này trước khi tạo tag `vX.Y.Z`. CI kiểm tra core profile deterministic;
các mục dưới đây kiểm tra phần cứng, provider online và audio mà GitHub-hosted runner
không thể xác minh.

## Release candidate

- [ ] Commit dự kiến phát hành đã nằm trên `main` và working tree sạch.
- [ ] CI của commit dự kiến phát hành đã xanh.
- [ ] Version tuân theo Semantic Versioning.
- [ ] Release notes mô tả thay đổi cho người dùng, lỗi đã sửa và giới hạn đã biết.
- [ ] Không stage `.env`, API key, MQTT credential, audio, transcript hoặc model cache.

## Runtime health

- [ ] Khởi động Mosquitto, MQTT vehicle simulator và ViVi từ terminal sạch.
- [ ] `/api/v1/health` báo đúng OpenAI, PhoWhisper, ZeroTTS, MQTT và SQLite.
- [ ] Có handbook SQLite và `voices/VIVI.zip`.
- [ ] Cấu hình lưu audio/transcript đúng với mục đích của buổi demo.

## Nghiệm thu demo

- [ ] Hội thoại bằng text trả phản hồi thành công.
- [ ] Microphone tạo transcript tiếng Việt đúng qua PhoWhisper.
- [ ] Câu hỏi cẩm nang trả lời có citation hợp lệ.
- [ ] HVAC, media và sưởi ghế cập nhật rồi verify đúng vehicle state.
- [ ] Cửa sổ và cửa xe luôn yêu cầu xác nhận trước khi thực thi.
- [ ] Confirmation bị từ chối, hết hạn hoặc replay không thực thi action.
- [ ] MQTT disconnect hoặc ACK không khớp không bao giờ được báo thành công.
- [ ] ZeroTTS phát được giọng Mai Chi; khi tắt voice vẫn dùng được text response.
- [ ] Sau khi restart, runtime trở lại trạng thái có thể demo.

## Phát hành và rollback

- [ ] Tạo annotated tag từ commit `main` đã nghiệm thu rồi push tag.
- [ ] GitHub Release workflow hoàn tất và publish `SHA256SUMS`.
- [ ] Giữ release tag trước đó làm điểm rollback.
- [ ] Một thành viên khác checkout được tag và chạy theo hướng dẫn trong README.
