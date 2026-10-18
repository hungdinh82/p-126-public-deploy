# Tài liệu ViVi — Chỉ mục

Bản đồ tài liệu của VIVI Cabin Copilot. Tài liệu lịch sử (bản nháp, review theo
ngày, runtime đã bị thay thế) nằm trong [`archive/`](archive/README.md).

## Bắt đầu nhanh

- [BRIEF.md](BRIEF.md) — one-page brief bài toán và giải pháp
- [PRD.md](PRD.md) — Product Requirements Document (G1 baseline)
- [REPOSITORY_STRUCTURE.md](REPOSITORY_STRUCTURE.md) — cấu trúc repo và nguyên tắc kiến trúc
- [RUN_LOCAL.md](RUN_LOCAL.md) — chạy ViVi trên máy cá nhân
- [setup_pc.md](setup_pc.md) · [google_api.md](google_api.md) — cấu hình môi trường và provider

## Kiến trúc & runtime hiện hành

- [architecture_diagram.md](architecture_diagram.md) ([architecture.html](architecture.html)) — kiến trúc hệ thống
- [langgraph_core_v2.md](langgraph_core_v2.md) — **lõi hội thoại Core v2 (runtime hiện hành)**
- [contracts.md](contracts.md) — runtime contracts
- [conversation_generalization_runtime.md](conversation_generalization_runtime.md) — hợp đồng hội thoại và địa điểm
- [conversation_open_world_plan.md](conversation_open_world_plan.md) — Conversation & Information Service
- [topic_guardrails.md](topic_guardrails.md) — chủ đề hội thoại và guardrails
- [memory.md](memory.md) — bộ nhớ hội thoại
- [local_tool_pipeline.md](local_tool_pipeline.md) — local tools và contract phía host

## Navigation & năng lượng

- [navigation_dialogue_scenarios.md](navigation_dialogue_scenarios.md) — kịch bản địa điểm, sạc, khởi hành
- [navigation_energy.md](navigation_energy.md) — dẫn đường và mô hình năng lượng

## RAG & handbook

- [rag_local.md](rag_local.md) — offline VF8 RAG trên AGX Xavier
- [handbook_sqlite.md](handbook_sqlite.md) — đưa handbook vào SQLite
- [agent_rag_architecture.md](agent_rag_architecture.md) — thiết kế agents và RAG

## Voice & model

- [voice_assistant.md](voice_assistant.md) — phản hồi nói và ranh giới intent
- [MODEL_STREAMING.md](MODEL_STREAMING.md) — tùy chọn streaming cho AI server

## Edge / Jetson

- [jetson_ai.md](jetson_ai.md) — STT/SLM/TTS server trên AGX Xavier
- [setup_jetson_nano.md](setup_jetson_nano.md) — setup trên Jetson Nano 4 GB
- [JETSON_MAIN_INTEGRATION.md](JETSON_MAIN_INTEGRATION.md) — tích hợp Jetson với lõi LangGraph
- [LAPTOP_JETSON_MIGRATION_PLAN.md](LAPTOP_JETSON_MIGRATION_PLAN.md) — kế hoạch Laptop Orchestrator + Jetson

## Chất lượng & vận hành

- [evaluation.md](evaluation.md) — evaluation evidence
- [UAT_FULL_FLOW.md](UAT_FULL_FLOW.md) — kịch bản UAT toàn luồng
- [VEHICLE_SIMULATOR_SPEC.md](VEHICLE_SIMULATOR_SPEC.md) — đặc tả vehicle simulator
- [UI_FLOW.md](UI_FLOW.md) — wireframe và UI flow
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) — checklist phát hành
- [AI_LOG_SETUP.md](AI_LOG_SETUP.md) — cấu hình AI log

## Học liệu

- [`guide/`](guide/) — cuốn sách hướng dẫn xây dựng dự án AI Agent (tài liệu tham khảo, độc lập với sản phẩm)
