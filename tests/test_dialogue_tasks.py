"""Episode-level regressions: actual tools and persisted state, not wording alone."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.agents.classifier import StructuredAPIIntentClassifier
from src.vivi.agents.graph import build_graph
from src.vivi.agents.tasks import active_task, pending_for_turn, resume_task
from tests.test_long_term_memory import invoke
from tests.test_long_term_memory import services as _services

services = _services


@pytest.mark.parametrize("yes", ["có", "đồng ý", "ừ", "ok", "được nhé"])
def test_offer_acceptance_executes_after_restart_once(services, yes):
    first = invoke(services, "hôm nay tôi buồn quá")
    assert first["output"]["execution"] is None
    pending = active_task(services.history.recent("driver", 6))
    assert pending and pending.follow_up.intent == "media.play"
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
    invoke(services, "hôm nay tôi buồn quá")
    history = services.history.recent("driver", 6)
    frame = history[-1]["diagnostics"]["pending_task"]
    frame["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert resume_task("có", history) == {"task_resolution": "expired"}
    assert resume_task("có", [{"answer": "Mình mở cửa nhé?"}]) == {}


def test_slot_completion_preserves_parameters_then_uses_real_confirmation(services):
    invoke(services, "mở cửa sổ 50%")
    pending = active_task(services.history.recent("driver", 6))
    assert pending.kind == "slot" and pending.follow_up.arguments.position_percent == 50
    second = invoke(services, "bên tài")["output"]
    assert second["status"] == "confirmation_required"
    third = invoke(services, "đồng ý")["output"]
    assert third["execution"]["verified"]
    assert third["vehicle_state"]["window_positions"]["driver"] == 50


def test_vehicle_confirmation_rechecks_current_safety(services):
    invoke(services, "mở cửa sổ bên tài")
    services.action_gateway.vehicle.set_driving("driver", True)
    result = invoke(services, "có")["output"]
    assert not result["execution"]["executed"]


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
    assert result["route"] == "conversation" and "sở thích" in result["response_text"]
    assert "Nam" not in result["response_text"]
    result = invoke(services, "xoá trí của bạn về tôi")["output"]
    assert result["route"] == "clarify" and services.memory.list("default")
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
            return json.dumps({"route": "conversation", "intent": "memory.remember",
                               "arguments": {"memory_key": "preferred_music", "memory_value": "jazz"},
                               "response_text": "Tôi đã mở cửa xe."})
    model_services = replace(services, classifier=StructuredAPIIntentClassifier(Client()))
    result = invoke(model_services, "Lưu sở thích nhạc jazz cho mình nhé")["output"]
    assert result["execution"] is None and "mở cửa" not in result["response_text"]
    assert services.memory.list("default")[0]["value"] == "jazz"


def test_model_reset_tool_only_creates_confirmation(services):
    invoke(services, "Từ nay gọi tôi là Nam")
    class Client:
        def generate_json(self, **kwargs):
            return '{"route":"conversation","intent":"memory.reset"}'
    model_services = replace(services, classifier=StructuredAPIIntentClassifier(Client()))
    result = invoke(model_services, "Bỏ hết những gì đã lưu về mình nhé")["output"]
    assert result["route"] == "clarify" and services.memory.list("default")
    invoke(model_services, "đồng ý")
    assert not services.memory.list("default")


def test_invalid_offer_cannot_become_an_executable_frame():
    state = {"turn_id": "one", "route": "conversation", "decision": {
        "follow_up": {"intent": "door.set_open", "arguments": {"open": True}}}}
    assert pending_for_turn(state) is None  # Missing zone.
    state["decision"]["follow_up"] = {"intent": "memory.reset"}
    assert pending_for_turn(state) is None  # Only the memory executor can propose reset.


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
