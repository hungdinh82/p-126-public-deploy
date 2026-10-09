"""Episode-level regressions: actual tools and persisted state, not wording alone."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.graphs import build_graph
from src.vivi.dialogue.contracts import SemanticTurn, SessionState
from src.vivi.dialogue.reducer import reduce_turn
from tests.test_long_term_memory import invoke
from tests.test_long_term_memory import services as _services

services = _services


@pytest.mark.parametrize("yes", ["có", "đồng ý", "ừ", "ok", "được nhé"])
def test_offer_acceptance_executes_after_restart_once(services, yes):
    first = invoke(services, "hôm nay tôi buồn quá")
    assert first["output"]["execution"] is None
    pending = first["session"]["active"]
    assert pending["steps"][0]["status"] == "offer" and pending["steps"][0]["draft"]["capability"] == "media.play"
    # invoke rebuilds the graph: the frame survives because it is persisted.
    second = invoke(services, yes)["output"]
    assert second["intent"] == "media.play" and second["execution"]["verified"]
    assert second["action_proposal"]["arguments"] == {}
    third = invoke(services, yes)["output"]
    assert third["execution"] is None


@pytest.mark.parametrize("interruption", ["không", "thôi", "xin chào", "dừng nhạc"])
def test_cancel_or_topic_switch_clears_offer(services, interruption):
    invoke(services, "hôm nay tôi buồn quá")
    invoke(services, interruption)
    result = invoke(services, "có")["output"]
    assert result["execution"] is None


def test_offer_is_session_and_profile_scoped(services):
    invoke(services, "hôm nay tôi buồn quá", session="alice")
    assert invoke(services, "có", session="bob")["output"]["execution"] is None
    other = replace(services, memory_profile_id="other")
    assert invoke(other, "có", session="alice")["output"]["execution"] is None


def test_expired_or_untyped_prose_never_authorizes_a_tool(services):
    first = invoke(services, "hôm nay tôi buồn quá")
    session = SessionState.model_validate(first["session"])
    session.active.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    state, message = reduce_turn(session, SemanticTurn(act="confirm", task_ref="active"))
    assert state.active is None and message
    # An old history record or prose cannot create a task in a new checkpoint.
    services.history.save({"session_id": "prose", "turn_id": "old", "query": "Mở cửa", "answer": "Mình mở cửa nhé?"})
    assert invoke(services, "có", session="prose")["execution"] is None


def test_slot_completion_preserves_parameters_then_uses_real_confirmation(services):
    first = invoke(services, "mở cửa sổ 50%")
    pending = first["session"]["active"]["steps"][0]
    assert pending["status"] == "waiting_slot" and pending["draft"]["arguments"]["position_percent"] == 50
    second = invoke(services, "bên tài")["output"]
    assert second["status"] == "confirmation_required"
    third = invoke(services, "đồng ý")["output"]
    assert third["execution"]["verified"]
    assert third["vehicle_state"]["window_positions"]["driver"] == 50


def test_vehicle_confirmation_rechecks_current_safety(services):
    invoke(services, "mở cửa sổ bên tài")
    services.action_gateway.vehicle.set_driving("driver", True)
    result = invoke(services, "có")["output"]
    assert result["status"] == "blocked" and not (result.get("execution") or {}).get("executed")


def test_temperature_slot_accepts_number_and_preserves_limits(services):
    invoke(services, "đặt nhiệt độ")
    result = invoke(services, "24")["output"]
    assert result["execution"]["verified"]
    assert result["vehicle_state"]["temperature_celsius"] == 24
    invoke(services, "đặt nhiệt độ")
    result = invoke(services, "99")["output"]
    assert not result.get("execution") or not result["execution"]["executed"]


def test_personal_memory_never_retrieves_and_reset_requires_acceptance(services):
    invoke(services, "Từ nay gọi tôi là Nam")
    result = invoke(services, "bạn biết mình thích gì không?")["output"]
    assert result["route"] == "conversation" and "chưa" in result["response_text"]
    assert "Nam" not in result["response_text"]
    result = invoke(services, "xoá trí của bạn về tôi")["output"]
    assert result["status"] == "confirmation_required" and services.memory.list("default")
    invoke(services, "không")
    assert services.memory.list("default")
    invoke(services, "xoá ghi nhớ của bạn về tôi")
    invoke(services, "có")
    assert services.memory.list("default") == []


def test_latest_complaint_is_request_not_negation_and_filler_is_not_song(services):
    result = invoke(services, "tôi bảo có sao bạn không mở nhạc luôn đi")["output"]
    assert result["intent"] == "media.play" and result["execution"]["verified"]
    assert result["action_proposal"]["arguments"] == {}
    result = invoke(services, "đừng mở nhạc")["output"]
    assert result["execution"] is None


def test_model_memory_tools_execute_instead_of_trusting_response_text(services):
    class Client:
        def generate_json(self, **kwargs):
            return json.dumps({"act": "request", "actions": [{"capability": "memory.remember",
                               "arguments": {"memory_key": "preferred_music", "memory_value": "jazz"}}],
                               "text": "Tôi đã mở cửa xe."})
    model_services = replace(services, semantic_client=Client())
    result = invoke(model_services, "Lưu sở thích nhạc jazz cho mình nhé")["output"]
    assert result["execution"] is None and "mở cửa" not in result["response_text"]
    assert services.memory.list("default")[0]["value"] == "jazz"


def test_model_reset_tool_only_creates_confirmation(services):
    invoke(services, "Từ nay gọi tôi là Nam")
    class Client:
        def generate_json(self, **kwargs):
            if "đồng ý" in kwargs["user"]:
                return '{"act":"confirm","task_ref":"active"}'
            return '{"act":"request","actions":[{"capability":"memory.reset"}]}'
    model_services = replace(services, semantic_client=Client())
    result = invoke(model_services, "Bỏ hết những gì đã lưu về mình nhé")["output"]
    assert result["status"] == "confirmation_required" and services.memory.list("default")
    invoke(model_services, "đồng ý")
    assert not services.memory.list("default")


def test_invalid_offer_cannot_execute_without_validation():
    from src.vivi.capabilities.registry import REGISTRY
    from src.vivi.dialogue.contracts import ActionDraft
    state, _ = reduce_turn(SessionState(), SemanticTurn(act="offer", actions=[ActionDraft(capability="door.set_open", arguments={"open": True})]))
    state, _ = reduce_turn(state, SemanticTurn(act="confirm", task_ref="active"))
    assert REGISTRY["door.set_open"].validate(state.active.steps[0].draft)[1]


@pytest.mark.asyncio
async def test_async_flow_resumes_offer_and_memory_reset(services):
    graph = build_graph(services)
    async def turn(text):
        return await graph.ainvoke({"session_id": "async-tasks", "input_text": text})
    await turn("hôm nay tôi buồn quá")
    result = await turn("có")
    assert result["output"]["execution"]["verified"]
    await turn("Từ nay gọi tôi là Nam")
    await turn("xoá ghi nhớ của bạn về tôi")
    await turn("đồng ý")
    assert not services.memory.list("default")
