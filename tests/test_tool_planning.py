"""Contract, context and semantic evidence regressions for the redesigned pipeline."""

import json

import pytest
from pydantic import ValidationError

from src.vivi.agents.action_validation import validate_action_arguments
from src.vivi.agents.classifier import RulesIntentClassifier, StructuredAPIIntentClassifier
from src.vivi.agents.context import resolve_request
from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.tools import TOOL_REGISTRY, validate_tool_arguments
from src.vivi.rag.generator import ExtractiveHandbookGenerator, validate_grounding
from src.vivi.rag.knowledge import analyze_question
from tests.test_history_regressions import chunk


@pytest.mark.parametrize(
    "query,key",
    [
        ("hỗ trợ giữ làn", "lane_keep"),
        ("ga tự động", "acc"),
        ("cửa sổ bên tài", "window"),
        ("sạc không dây điện thoại", "phone_charging"),
        ("sạc nhanh DC", "ev_charging"),
        ("SRS của VF8", "airbag"),
        ("Trẻ em nên ngồi ở đâu?", "child_safety"),
    ],
)
def test_same_topic_contract_for_routing_and_rag(query, key):
    assert analyze_question(query).topic.key == key
    assert resolve_request(query, []).topic == key


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("window.set_position", {"position_percent": True, "zone": "driver"}),
        ("door.set_open", {"open": "false", "zone": "driver"}),
        ("seat.set_heat_level", {"level": 1.5, "zone": "driver"}),
        ("vehicle.get_status", {"value_celsius": 20}),
        ("climate.set_temperature", {"value_celsius": 20, "locked": True}),
    ],
)
def test_tool_contract_rejects_coercion_or_cross_tool_parameters(name, arguments):
    result, error = validate_tool_arguments(name, arguments)
    assert result is None and error


def test_decision_preserves_strict_boolean_boundary_before_tool_validation():
    with pytest.raises(ValidationError):
        IntentDecision(route="action", intent="door.set_lock", arguments={"locked": "false", "zone": "driver"})
    decision = IntentDecision(route="action", intent="media.pause", arguments={"locked": False})
    assert validate_action_arguments(decision)[0] is None


def test_model_prompt_exposes_actual_tool_schemas_and_resolved_request():
    class Client:
        def generate_json(self, **kwargs):
            self.prompt = kwargs["user"]
            return json.dumps({"route": "handbook", "intent": "manual.search", "arguments": {"query": "tính năng đấy"}})

    client = Client()
    history = [{"route": "handbook", "query": "ADAS là gì?"}]
    classifier = StructuredAPIIntentClassifier(client)
    result = classifier.classify("giới thiệu tính năng đấy", history)
    assert "ADAS" in result.arguments.query
    # An unresolved request uses the model and receives the same registry.
    classifier.classify("Đi cùng mình nhé", [])
    assert "additionalProperties" in client.prompt and "position_percent" in client.prompt
    assert {"memory.recall", "memory.remember", "memory.forget", "memory.reset"} <= TOOL_REGISTRY.keys()


def test_new_topic_or_intervening_action_ends_old_knowledge_context():
    history = [{"route": "handbook", "query": "ADAS là gì?"}]
    assert resolve_request("Còn dung lượng pin?", history).topic == "battery"
    assert "ADAS" not in resolve_request("Còn dung lượng pin?", history).text
    history.append({"route": "action", "intent": "media.pause", "query": "dừng nhạc"})
    assert resolve_request("tính năng đấy là gì?", history).topic is None


def test_pending_slot_only_accepts_a_slot_reply():
    history = [{"route": "clarify", "query": "mở cửa sổ", "answer": "Bạn muốn mở cửa sổ bên nào?"}]
    assert RulesIntentClassifier().classify("bên phụ", history).arguments.zone == "front_passenger"
    assert RulesIntentClassifier().classify("bên phụ đang bị bẩn", history).route == "clarify"


def test_definition_subject_is_requested_feature_not_a_related_feature():
    source = chunk(
        "lane",
        "Cảnh báo chệch làn (LDW) là hệ thống cảnh báo bằng âm thanh. "
        "LDW hoạt động khi hỗ trợ giữ làn (LKA) bật. "
        "Hỗ trợ giữ làn (LKA) là hệ thống hỗ trợ duy trì xe trong làn hiện tại.",
        "Hệ thống trợ làn",
    )
    result = ExtractiveHandbookGenerator().generate("Giữ làn là gì?", [source], [])
    assert "duy trì" in result.answer and "âm thanh" not in result.answer
    assert validate_grounding(result, [source]) == (True, None)


def test_availability_does_not_return_operating_instruction_or_claim_actual_equipment():
    generator = ExtractiveHandbookGenerator()
    button = chunk(
        "button",
        "Nhấn nút ACC để kích hoạt kiểm soát hành trình. Khi ACC hoạt động, người lái chỉ kiểm soát vô lăng.",
        "Vận hành ACC",
    )
    assert generator.generate("VF8 có cruise control không?", [button], []).insufficient_evidence
    definition = chunk(
        "definition",
        "Kiểm soát hành trình thích ứng (ACC) là hệ thống giúp giữ khoảng cách với xe trước.",
        "ACC (nếu được trang bị)",
    )
    result = generator.generate("VF8 có cruise control không?", [button, definition], [])
    assert "tuỳ phiên bản" in result.answer and "Nhấn nút" not in result.answer


def test_umbrella_overview_is_derived_from_sources_not_a_fixed_adas_reply():
    generator = ExtractiveHandbookGenerator()
    source = chunk(
        "features", "Các tính năng ADAS bao gồm hỗ trợ giữ làn đường (LKA) và phanh khẩn cấp (AEB).", "Chức năng ADAS"
    )
    result = generator.generate("ADAS là gì?", [source], [])
    assert "LKA" in result.answer and "AEB" in result.answer and "ACC" not in result.answer
    assert result.claims[0].source_ids == ["features"]
    assert generator.generate("ADAS là gì?", [], []).insufficient_evidence


@pytest.mark.parametrize("query", ["không có gì, bạn chạy tiếp đi", "thôi bỏ qua nhé"])
def test_acknowledgement_never_requests_a_new_task(query):
    result = RulesIntentClassifier().classify(query, [])
    assert result.route == "conversation" and "?" not in result.response_text


def test_dialogue_cancellation_is_not_a_prefix_match_for_an_unrelated_sentence():
    result = RulesIntentClassifier().classify("Thời gian nghỉ là bao lâu?", [])
    assert result.route != "conversation"


def test_implicit_subject_procedure_is_not_deleted_by_the_retrieval_topic_gate():
    from src.vivi.rag.relevance import matches_topic

    source = chunk("procedure", "Dừng xe an toàn rồi nhấn nút nguồn để khởi động.", "Khởi động xe")
    assert matches_topic("Làm sao khởi động xe bằng chìa khoá?", source)


def test_control_instruction_question_is_not_an_unsupported_command():
    result = RulesIntentClassifier().classify("Chỉnh gương thì dùng nút nào vậy?", [])
    assert result.intent == "manual.search"


def test_conditional_temperature_change_is_not_executed_as_an_immediate_request():
    result = RulesIntentClassifier().classify_with_context(
        "Nếu tôi lạnh thì tăng nhiệt độ thêm 2 độ", [], {"temperature_celsius": 23}
    )
    assert result.route == "clarify"


def test_function_explanation_can_describe_prevention_without_a_copula():
    source = chunk("switch", "Nút khoá cửa sổ ngăn hành khách phía sau điều khiển cửa sổ.", "Cửa sổ điện")
    result = ExtractiveHandbookGenerator().generate("Công tắc khóa cửa sổ để làm gì?", [source], [])
    assert "ngăn" in result.answer and not result.insufficient_evidence


def test_documentation_is_not_a_requested_source_code_artifact():
    source = chunk("battery", "Tính năng phân tích dữ liệu pin và hành vi sạc.", "Pin")
    result = ExtractiveHandbookGenerator().generate("Mã nguồn firmware của pin?", [source], [])
    assert result.insufficient_evidence


def test_categorical_answer_does_not_append_a_different_category():
    source = chunk(
        "child",
        "Ghế trẻ em quay mặt phía sau phù hợp cho trẻ sơ sinh. Ghế trẻ em hướng về phía trước phù hợp cho trẻ lớn hơn.",
        "Ghế trẻ em",
    )
    result = ExtractiveHandbookGenerator().generate("Trẻ sơ sinh cần loại ghế nào?", [source], [])
    assert "phía sau" in result.answer and "phía trước" not in result.answer
