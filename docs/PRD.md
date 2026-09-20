# Product Requirements Document — VIVI Cabin Copilot

> **Phiên bản:** G1 v0.1  
> **Trạng thái:** Design baseline  
> **Mã đề:** DEV-01 · Team P-126

## 1. Product vision

VIVI Cabin Copilot là trợ lý cabin tiếng Việt offline-first chạy trên edge. Sản phẩm giúp tài xế điều khiển các chức năng cabin mô phỏng và tra cứu sổ tay có căn cứ mà không để LLM trực tiếp quyết định hành động vật lý.

## 2. Mục tiêu và nguyên tắc

1. Core voice-to-response path hoạt động khi không có WAN.
2. Mọi action đi qua typed schema, deterministic policy và live vehicle state.
3. Handbook answer có citation hoặc từ chối khi thiếu bằng chứng.
4. Mọi request có trace để đo latency, chất lượng và failure mode.
5. Hệ thống chỉ điều khiển simulator; không có real CAN adapter.

## 3. Personas

### Driver

Muốn thao tác rảnh tay, nhận phản hồi ngắn và biết chính xác hệ thống đã nghe, hiểu và thực hiện điều gì. Không cần hiểu chi tiết model hoặc hạ tầng.

### System engineer

Muốn xem health, trace, lỗi, latency, model/knowledge version và audit của các quyết định an toàn. Không được phép dùng dashboard để bỏ qua local safety.

## 4. User journeys ưu tiên

| ID | User story | Acceptance summary |
|---|---|---|
| US-01 | Là tài xế, tôi muốn nói hoặc gõ “đặt điều hòa 24 độ”. | Tool chỉ báo thành công sau MQTT acknowledgement và state = 24°C. |
| US-02 | Là tài xế, tôi muốn hỏi ý nghĩa một cảnh báo trong sổ tay. | Trả lời đúng model/version, có page/section citation hoặc abstain. |
| US-03 | Là tài xế, tôi muốn mở cửa/kính nhưng phải thấy chính xác hành động trước khi xác nhận. | Approval gắn action, session, expiry; deny/timeout/replay không thực thi. |
| US-04 | Là tài xế, tôi muốn được hỏi lại khi hệ thống nghe không chắc chắn. | Không tự thực thi dưới confidence threshold; có text fallback. |
| US-05 | Là tài xế, tôi muốn core functions tiếp tục hoạt động khi mất mạng. | Core regression pass khi WAN bị chặn. |
| US-06 | Là kỹ sư, tôi muốn xem request trace và lỗi theo từng stage. | Trace có chung `trace_id`, timing và outcome cho STT/router/model/policy/tool/TTS. |

## 5. Functional requirements

### FR-1 — Identity và roles

- Hỗ trợ driver và system engineer.
- Engineer-only endpoints từ chối token driver.
- G1 chấp nhận demo accounts; production identity không thuộc phạm vi.

### FR-2 — Input và speech

- Nhận text và push-to-talk audio tiếng Việt.
- Hiển thị transcript trước hoặc cùng lúc xử lý.
- Có VAD, STT confidence và text fallback.
- Wake word là P2, không chặn MVP.

### FR-3 — Intent routing

- Phân loại ít nhất: conversation, handbook, vehicle command, unsupported.
- Core vehicle catalog tối thiểu gồm HVAC, media, seat comfort, window và door simulation.
- Low-confidence hoặc thiếu entity phải hỏi lại thay vì đoán.
- Kế hoạch có typed steps, allowlist và giới hạn số bước.

### FR-4 — Vehicle digital twin

- Gửi command qua MQTT, không gọi CAN thật.
- Simulator cung cấp state, acknowledgement, timeout và fault injection.
- Command có correlation ID, expiry và idempotency key.
- Chỉ acknowledgement phù hợp mới được dùng để báo thành công.

### FR-5 — Safety và HITL

| Class | Phạm vi | Chính sách |
|---|---|---|
| R0 | Đọc trạng thái, hỏi sổ tay, preview route | Không cần confirmation. |
| R1 | HVAC, media, seat comfort trong giới hạn | Validate role, args và live state rồi thực thi. |
| R2 | Cửa và kính | Hiển thị/nói action chính xác; confirmation single-use có expiry. |
| R3 | Phanh, lái, truyền động, tắt safety | Tool không tồn tại hoặc luôn bị từ chối và audit. |

- Safety là deterministic code, không phải quyết định của LLM.
- Policy đọc live vehicle state ngay trước execution.
- Approval cũ không được tái sử dụng hoặc sửa tham số.

### FR-6 — Grounded handbook RAG

- Ingest tài liệu có quyền sử dụng và lưu model/version/page/section metadata.
- Query lọc đúng dòng và phiên bản xe trước retrieval.
- Material claims phải ánh xạ tới retrieved chunks.
- Không đủ hoặc mâu thuẫn bằng chứng phải abstain.
- Nội dung trong handbook được coi là data, không phải system instruction.

### FR-7 — Response và error handling

- Phản hồi ngắn, voice-first, có thể hủy TTS.
- Nêu rõ khi yêu cầu bị từ chối, cần xác nhận hoặc không đủ dữ liệu.
- STT/SLM/TTS crash, MQTT disconnect, timeout và corrupt index đều có fallback hiển thị cho người dùng.

### FR-8 — Engineer observability

- Hiển thị model/version/quantization, health và offline state.
- Hiển thị latency p50/p95, intent result, policy decision, tool outcome và grounding evidence.
- Log redact raw audio và sensitive text theo mặc định.

## 6. Non-functional requirements

| ID | Yêu cầu |
|---|---|
| NFR-1 | Core suite chạy với WAN disabled. |
| NFR-2 | Voice p95 mục tiêu ≤ 2.500 ms; handbook p95 ≤ 4.000 ms trên hardware được công bố. |
| NFR-3 | Không có đường đi từ UI/LLM tới MQTT mà bỏ qua policy gateway. |
| NFR-4 | Invalid schema không làm crash API hoặc simulator. |
| NFR-5 | Model, dataset, handbook index, hardware và commit phải được ghi trong eval report. |
| NFR-6 | Raw audio retention là opt-in; trip memory có thể xóa và không thay thế live state. |
| NFR-7 | Clean checkout có hướng dẫn chạy, test và khôi phục lỗi. |

## 7. Scope và ưu tiên

### P0 — Must ship

- Hai roles ở mức demo.
- Text/voice với text fallback.
- Single-step command cho ít nhất năm intents.
- MQTT vehicle twin và verified acknowledgement.
- Grounded RAG citation/abstention.
- R0-R3 policy và HITL cửa/kính.
- Local Q4 và offline regression.
- Intent, grounding, tool và latency evidence.
- Docker, tests và reproducible setup.

### P1 — Should ship

- Bounded multi-step command.
- Q4/Q8 comparison.
- Trip-scoped memory.
- Engineering dashboard đầy đủ.
- LangGraph specialist nodes.

### P2 — Stretch

- Wake word và richer TTS streaming.
- Local POI/map pack.
- Camera/multimodal vision spike.
- Fleet monitoring và simulated signed OTA rollback.

### Out of scope

- Xe thật, CAN adapter, phanh, lái hoặc propulsion.
- Production authentication và automotive certification.
- Voice biometric approval.
- Cloud service nằm trên critical path của core controls/RAG.

## 8. Success metrics và evaluation contract

| Metric | Target | Điều kiện báo cáo |
|---|---|---|
| Intent macro F1 | ≥ 0,90 | Frozen Vietnamese set; báo per-class score và confusion matrix. |
| Entity accuracy | Báo baseline và failure classes | Bao gồm nhiệt độ, vị trí, cửa/kính và noisy transcript. |
| Valid tool completion | ≥ 0,95 | Tính từ simulator acknowledgement, không từ text của model. |
| R2 safety | 100% confirmation; 0 execution khi deny/timeout/replay | Có negative tests. |
| Citation precision | ≥ 0,95 | Source mapping được kiểm tra trên frozen set. |
| Unsupported-answer rate | ≤ 0,05 | Bao gồm unanswerable và wrong-version questions. |
| Voice latency | p95 ≤ 2.500 ms | Từ end-of-speech tới first audible response. |
| Offline capability | 100% core regression pass | WAN bị chặn rõ ràng trong test. |

Nếu target latency không đạt trên hardware đã chọn, Gate cuối chấp nhận báo cáo trung thực gồm bottleneck, variance và remediation plan; không được thay số liệu bằng constant.

## 9. Assumptions, dependencies và risks

- Nhóm có tối thiểu một máy đủ chạy local Q4; Jetson Nano/Orin là target profile, không phải cam kết trước benchmark.
- Handbook mẫu phải có quyền sử dụng và giữ được page metadata.
- POI offline dùng fixture nhỏ nếu cần demo; không giả định có API VinFast.
- Scope pressure được xử lý bằng cách dừng P2 và thu nhỏ P1, không cắt safety/RAG evidence.
- “Multimodal” trong P0 nghĩa là voice + touch/text; vision chỉ được công bố nếu P2 spike thực sự hoàn thành.

## 10. Release acceptance cho MVP

- Một reviewer có thể clone, setup và chạy documented commands.
- Năm intent text/voice điều khiển simulator và xác minh state.
- Cửa/kính không thể chạy nếu thiếu confirmation mới và hợp lệ.
- Handbook trả citation đúng hoặc abstain.
- Core regression pass khi WAN disabled.
- Eval report ghi rõ dữ liệu, model, quantization, hardware và commit.
- Critical safety, data-loss và demo-blocking defects đã đóng.

## 11. Liên kết thiết kế

- [One-page Brief](BRIEF.md)
- [Wireframe và UI Flow](UI_FLOW.md)
- [Kiến trúc hệ thống](architecture_diagram.md)
- [Kế hoạch năm tuần](project_plan_5_weeks.md)
