"""Contract, context and semantic evidence regressions for the redesigned pipeline."""

import pytest
from pydantic import ValidationError

from src.vivi.agents.tools import TOOL_REGISTRY, validate_tool_arguments
from src.vivi.dialogue.contracts import ActionDraft, SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.rag.generator import ExtractiveHandbookGenerator, validate_grounding
from tests.support.history_regressions import chunk


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


@pytest.mark.parametrize(
    "name,arguments,question",
    [
        (
            "door.set_open",
            {"open": True},
            "Bạn muốn mở cửa nào: bên tài, bên phụ, sau trái, sau phải hoặc tất cả?",
        ),
        (
            "window.set_position",
            {"position_percent": 100},
            "Bạn muốn điều chỉnh cửa sổ nào: bên tài, bên phụ, sau trái, sau phải hoặc tất cả?",
        ),
        (
            "door.set_lock",
            {"locked": True},
            "Bạn muốn khoá hay mở khoá cửa nào: bên tài, bên phụ, sau trái, sau phải hoặc tất cả?",
        ),
        (
            "seat.set_heat_level",
            {"level": 2},
            "Bạn muốn điều chỉnh sưởi ghế nào: bên tài, bên phụ, sau trái, sau phải hoặc tất cả?",
        ),
    ],
)
def test_missing_cabin_target_prompts_with_supported_choices(name, arguments, question):
    result, error = validate_tool_arguments(name, arguments)
    assert result is None
    assert error == question


def test_decision_preserves_strict_boolean_boundary_before_tool_validation():
    with pytest.raises(ValidationError):
        ActionDraft(capability="door.set_lock", arguments={"locked": "false", "zone": "driver"})
    with pytest.raises(ValidationError):
        ActionDraft(capability="media.pause", arguments={"locked": False})
    with pytest.raises(ValidationError):
        ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": True})


@pytest.mark.asyncio
async def test_model_prompt_exposes_actual_tool_schemas_without_rewriting_question():
    class Client:
        async def agenerate_json(self, **kwargs):
            self.prompt = kwargs["user"]
            return '{"act":"question","actions":[{"capability":"manual.search","arguments":{"query":"tính năng đấy"}}]}'

    client = Client()
    resolver = SemanticResolver({"google": ModelProvider("google", client, 1)})
    from src.vivi.capabilities.registry import catalog

    value, _, _ = await resolver.resolve(
        "giới thiệu tính năng đấy",
        SessionState(),
        requested="google",
        history=[{"route": "handbook", "query": "ADAS là gì?"}],
        catalog=catalog(),
    )
    assert value.actions[0].arguments.query == "tính năng đấy"  # Model owns interpretation.
    assert "ADAS" in client.prompt and "position_percent" in client.prompt
    assert {"memory.recall", "memory.remember", "memory.forget", "memory.reset"} <= TOOL_REGISTRY.keys()


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


def test_implicit_subject_procedure_is_not_deleted_by_the_retrieval_topic_gate():
    from src.vivi.rag.relevance import matches_topic

    source = chunk("procedure", "Dừng xe an toàn rồi nhấn nút nguồn để khởi động.", "Khởi động xe")
    assert matches_topic("Làm sao khởi động xe bằng chìa khoá?", source)


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
