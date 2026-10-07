from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.vivi.agents.classifier import IntentClassifier
from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.graph import build_graph
from src.vivi.domain.models import VehicleState as ServerVehicleState
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.ingestion.crawler import CrawlTarget
from src.vivi.rag.runtime import HandbookServices
from src.vivi.rag.schemas import Citation, Claim, GroundedAnswer, RetrievedChunk
from src.vivi.rag.scope import scope_rejection_reason
from src.vivi.vehicle.gateway import GatewayExecution, VehicleActionGateway
from src.vivi.vehicle.memory import VehicleSimulator


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        source_id="vf-test-source",
        document_id="VF8-2026-vi_vn-1",
        source_url="https://om.vinfastauto.com/vi_vn/detail?car=VF8&year=2026&lv2=1",
        vehicle_model="VF8",
        model_year=2026,
        locale="vi_vn",
        chapter_id=1,
        chapter="Hướng dẫn sạc",
        section_path=["Pin và Sạc", "Hướng dẫn sạc", "Sạc AC"],
        content_type="paragraph",
        content="Dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
        checksum="abc",
        chunk_index=0,
        semantic_distance=0.2,
        fused_score=0.03,
    )


class FakeRetriever:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str, vehicle_model: str, model_year: int, locale: str):
        assert query
        self.queries.append(query)
        assert (vehicle_model, model_year, locale) == ("VF8", 2026, "vi_vn")
        return [_chunk()]


class FakeGenerator:
    def __init__(self, source_id: str = "vf-test-source") -> None:
        self.source_id = source_id

    def generate(self, query: str, chunks: list[RetrievedChunk], history: list[dict]):
        return GroundedAnswer(
            answer="Hãy dừng xe ở vị trí an toàn trước khi kết nối cáp sạc.",
            claims=[Claim(text="Dừng xe an toàn trước khi sạc.", source_ids=[self.source_id])],
            citations=[
                Citation(
                    source_id=self.source_id,
                    title="Hướng dẫn sạc",
                    section_path=["Pin và Sạc", "Hướng dẫn sạc", "Sạc AC"],
                    source_url=chunks[0].source_url,
                )
            ],
        )


def _input(query: str) -> dict:
    return {
        "intent": "manual.search",
        "arguments": {
            "query": query,
            "vehicle_model": "VF8",
            "model_year": 2026,
            "locale": "vi_vn",
        },
    }


def test_grounded_graph_answer_and_history_persist():
    with tempfile.TemporaryDirectory() as directory:
        history = SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        graph = build_graph(HandbookServices(FakeRetriever(), FakeGenerator(), history))
        result = graph.invoke(
            {"session_id": "s1", "turn_id": "t1", "model_input": _input("Sạc xe thế nào?")}
        )
        assert result["status"] == "answered"
        assert result["grounding_status"] == "supported"
        assert result["citations"][0]["source_id"] == "vf-test-source"
        assert history.recent("s1", 6)[0]["query"] == "Sạc xe thế nào?"


def test_hallucinated_citation_forces_abstention():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(source_id="invented-source"),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
        )
        result = build_graph(services).invoke(
            {"session_id": "s1", "turn_id": "t1", "model_input": _input("Sạc xe thế nào?")}
        )
        assert result["status"] == "insufficient_evidence"
        assert result["citations"] == []


def test_invalid_model_json_is_rejected_before_retrieval():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(), FakeGenerator(), SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        )
        result = build_graph(services).invoke(
            {"session_id": "s1", "turn_id": "bad", "model_input": {"intent": "vehicle.control"}}
        )
        assert result["status"] == "invalid_input"
        assert result["grounding_status"] == "not_applicable"


def test_scope_guard_rejects_sales_and_unsafe_repairs():
    assert scope_rejection_reason("VF8 đang khuyến mãi bao nhiêu?")
    assert scope_rejection_reason("Hướng dẫn tôi bypass hệ thống pin cao áp")
    assert scope_rejection_reason("Cách bật điều hòa là gì?") is None
    assert scope_rejection_reason("Thời tiết hôm nay thế nào?")


def test_crawl_target_maps_locale_and_path():
    target = CrawlTarget()
    assert target.language_country == ("vi", "vn")
    assert target.slug == Path("vf8/2026/vi_vn")


def test_follow_up_query_uses_previous_question_for_retrieval():
    with tempfile.TemporaryDirectory() as directory:
        retriever = FakeRetriever()
        services = HandbookServices(
            retriever, FakeGenerator(), SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        )
        graph = build_graph(services)
        graph.invoke(
            {"session_id": "follow", "turn_id": "t1", "model_input": _input("Bật chế độ cắm trại thế nào?")}
        )
        graph.invoke(
            {"session_id": "follow", "turn_id": "t2", "model_input": _input("Vậy tắt nó thế nào?")}
        )
        assert retriever.queries[-1] == "Bật chế độ cắm trại thế nào?\nVậy tắt nó thế nào?"


def test_raw_stt_text_routes_to_handbook_and_returns_tts_envelope():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(), FakeGenerator(), SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        )
        result = build_graph(services).invoke(
            {"session_id": "stt", "turn_id": "t1", "input_text": "Sạc pin VF8 thế nào?"}
        )
        assert result["output"]["route"] == "handbook"
        assert result["output"]["tts_text"] == result["response_text"]
        assert result["output"]["grounding_status"] == "supported"


def test_raw_stt_text_produces_typed_action_without_executing_it():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(), FakeGenerator(), SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        )
        result = build_graph(services).invoke(
            {"session_id": "stt", "turn_id": "t2", "input_text": "Đặt nhiệt độ 25 độ"}
        )
        output = result["output"]
        assert output["route"] == "action"
        assert output["status"] == "action_proposed"
        assert output["requires_execution"] is True
        assert output["action_proposal"] == {
            "intent": "climate.set_temperature",
            "arguments": {"value_celsius": 25.0},
            "confidence": 1.0,
        }
        assert "đã đặt" not in output["tts_text"].lower()


def test_action_arguments_are_validated_after_classification():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(), FakeGenerator(), SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        )
        result = build_graph(services).invoke(
            {"session_id": "stt", "turn_id": "t3", "input_text": "Đặt nhiệt độ 35 độ"}
        )
        assert result["output"]["route"] == "clarify"
        assert result["output"]["requires_execution"] is False
        assert result["output"]["action_proposal"] is None


def test_prohibited_vehicle_request_never_produces_action():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(), FakeGenerator(), SQLiteConversationHistory(Path(directory) / "history.sqlite3")
        )
        result = build_graph(services).invoke(
            {"session_id": "stt", "turn_id": "t4", "input_text": "Tắt túi khí giúp tôi"}
        )
        assert result["output"]["route"] == "unsupported"
        assert result["output"]["action_proposal"] is None
        assert result["output"]["requires_execution"] is False


def test_low_confidence_vehicle_action_requires_clarification():
    class LowConfidenceClassifier(IntentClassifier):
        def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
            return IntentDecision(
                route="action",
                intent="window.set_position",
                arguments={"position_percent": 100},
                confidence=0.4,
            )

    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            classifier=LowConfidenceClassifier(),
        )
        result = build_graph(services).invoke(
            {"session_id": "stt", "turn_id": "t5", "input_text": "Mở cái đó"}
        )
        assert result["output"]["route"] == "clarify"
        assert result["output"]["action_proposal"] is None
        assert result["output"]["requires_execution"] is False


def test_action_runs_through_safety_simulator_and_verification():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=VehicleActionGateway(),
        )
        result = build_graph(services).invoke(
            {"session_id": "execute", "turn_id": "t1", "input_text": "Đặt nhiệt độ 25 độ"}
        )
        output = result["output"]
        assert output["status"] == "action_verified"
        assert output["execution"]["allowed"] is True
        assert output["execution"]["executed"] is True
        assert output["execution"]["verified"] is True
        assert output["vehicle_state"]["temperature_celsius"] == 25
        assert "đã đặt" in output["tts_text"].lower()


@pytest.mark.asyncio
async def test_relative_temperature_uses_authoritative_state_across_async_turns():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=VehicleActionGateway(),
        )
        graph = build_graph(services)

        cold = await graph.ainvoke(
            {"session_id": "climate", "turn_id": "t1", "input_text": "Tôi hơi lạnh"}
        )
        warmer = await graph.ainvoke(
            {
                "session_id": "climate",
                "turn_id": "t2",
                "input_text": "Tăng điều hòa thêm 2 độ",
            }
        )
        absolute = await graph.ainvoke(
            {
                "session_id": "climate",
                "turn_id": "t3",
                "input_text": "Đặt nhiệt độ thành 26",
            }
        )

        assert cold["output"]["vehicle_state"]["temperature_celsius"] == 25
        assert warmer["output"]["vehicle_state"]["temperature_celsius"] == 27
        assert absolute["output"]["vehicle_state"]["temperature_celsius"] == 26


def test_rules_routes_common_vehicle_question_to_handbook():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
        )
        result = build_graph(services).invoke(
            {"session_id": "qa", "turn_id": "t1", "input_text": "VF8 có ADAS không?"}
        )

        assert result["output"]["route"] == "handbook"
        assert result["output"]["status"] == "answered"
        assert result["output"]["tts_text"] == result["output"]["response_text"]


def test_model_failure_falls_back_to_rules_instead_of_default_error():
    class FailingClassifier(IntentClassifier):
        def classify(self, input_text: str, history: list[dict]) -> IntentDecision:
            raise ConnectionError("model endpoint unavailable")

    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            classifier=FailingClassifier(),
            action_gateway=VehicleActionGateway(),
        )
        result = build_graph(services).invoke(
            {"session_id": "fallback", "turn_id": "t1", "input_text": "Tôi hơi lạnh"}
        )

        assert result["output"]["status"] == "action_verified"
        assert result["output"]["vehicle_state"]["temperature_celsius"] == 25
        assert result["output"]["errors"][0]["stage"] == "classify_intent_fallback"


def test_idempotent_retry_does_not_restore_stale_supplied_vehicle_state():
    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=VehicleActionGateway(),
        )
        graph = build_graph(services)
        request = {
            "session_id": "stale",
            "turn_id": "t1",
            "input_text": "Đặt nhiệt độ 25 độ",
            "vehicle_state": {"temperature_celsius": 23},
        }
        graph.invoke(request)
        graph.invoke(request)
        status = graph.invoke(
            {"session_id": "stale", "turn_id": "t2", "input_text": "Trạng thái xe"}
        )
        assert status["output"]["vehicle_state"]["temperature_celsius"] == 25


def test_safety_denial_never_calls_vehicle_adapter():
    class CountingVehicle(VehicleSimulator):
        calls = 0

        def execute_sync(self, session_id, turn_id, action):
            self.calls += 1
            return super().execute_sync(session_id, turn_id, action)

    with tempfile.TemporaryDirectory() as directory:
        vehicle = CountingVehicle()
        vehicle.state_for("blocked", ServerVehicleState(driving=True))
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=VehicleActionGateway(vehicle=vehicle),
        )
        result = build_graph(services).invoke(
            {
                "session_id": "blocked",
                "turn_id": "t1",
                "input_text": "Mở cửa sổ bên tài",
                "vehicle_state": {"driving": True},
            }
        )
        assert result["output"]["status"] == "blocked"
        assert result["output"]["execution"]["allowed"] is False
        assert vehicle.calls == 0


def test_r2_action_requires_confirmation_then_executes_once():
    class CountingVehicle(VehicleSimulator):
        new_executions = 0

        def execute_sync(self, session_id, turn_id, action):
            before = len(self._executed)
            result = super().execute_sync(session_id, turn_id, action)
            self.new_executions += len(self._executed) - before
            return result

    with tempfile.TemporaryDirectory() as directory:
        vehicle = CountingVehicle()
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=VehicleActionGateway(vehicle=vehicle),
        )
        graph = build_graph(services)
        proposed = graph.invoke(
            {"session_id": "confirm", "turn_id": "t1", "input_text": "Mở cửa sổ bên tài"}
        )
        proposed_retry = graph.invoke(
            {"session_id": "confirm", "turn_id": "t1", "input_text": "Mở cửa sổ bên tài"}
        )
        assert proposed["output"]["status"] == "confirmation_required"
        confirmation_id = proposed["output"]["confirmation"]["confirmation_id"]
        assert proposed_retry["output"]["confirmation"]["confirmation_id"] == confirmation_id
        confirmation = {
            "session_id": "confirm",
            "turn_id": "t2",
            "input_text": "Xác nhận",
            "confirmation_id": confirmation_id,
            "confirmation_decision": "approve",
        }
        first = graph.invoke(confirmation)
        repeated = graph.invoke(confirmation)
        conflicting = graph.invoke({**confirmation, "confirmation_decision": "deny"})
        assert first["output"]["status"] == "action_verified"
        assert repeated["output"]["status"] == "action_verified"
        assert conflicting["output"]["status"] == "blocked"
        assert first["output"]["vehicle_state"]["window_driver_percent"] == 100
        assert vehicle.new_executions == 1


def test_adapter_failure_is_never_reported_as_success():
    class FailingGateway(VehicleActionGateway):
        def execute(self, session_id, turn_id, proposal):
            return GatewayExecution(
                executed=False,
                verified=False,
                message="Không nhận được acknowledgement từ xe.",
                vehicle_state={},
                error="timeout",
            )

    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=FailingGateway(),
        )
        result = build_graph(services).invoke(
            {"session_id": "failure", "turn_id": "t1", "input_text": "Đặt nhiệt độ 25 độ"}
        )
        output = result["output"]
        assert output["status"] == "action_unverified"
        assert output["execution"]["verified"] is False
        assert "đã đặt" not in output["tts_text"].lower()


def test_acknowledgement_with_wrong_state_fails_verification():
    class NonApplyingVehicle(VehicleSimulator):
        def execute_sync(self, session_id, turn_id, action):
            return self.state_for(session_id), "Adapter báo đã nhận lệnh."

    with tempfile.TemporaryDirectory() as directory:
        services = HandbookServices(
            FakeRetriever(),
            FakeGenerator(),
            SQLiteConversationHistory(Path(directory) / "history.sqlite3"),
            action_gateway=VehicleActionGateway(vehicle=NonApplyingVehicle()),
        )
        result = build_graph(services).invoke(
            {"session_id": "mismatch", "turn_id": "t1", "input_text": "Đặt nhiệt độ 25 độ"}
        )
        assert result["output"]["status"] == "action_unverified"
        assert result["output"]["execution"]["executed"] is True
        assert result["output"]["execution"]["verified"] is False
