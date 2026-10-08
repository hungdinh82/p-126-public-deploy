"""Native tool transport and normalization; no model or external services required."""

import json

import httpx
import pytest

from src.vivi.agents.classifier import LocalIntentClassifier
from src.vivi.agents.native_tools import decision_from_call
from src.vivi.config import Settings
from src.vivi.structured_llm import StructuredChatClient


def client_for(handler):
    classifier = LocalIntentClassifier(Settings(local_llm_model="test-model"))
    classifier.client = StructuredChatClient(
        base_url="http://local.test/v1", api_key="", model="test-model",
        timeout_seconds=1, max_attempts=1, transport=httpx.MockTransport(handler),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return classifier


def response(name, arguments):
    return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
        {"function": {"name": name, "arguments": json.dumps(arguments)}}
    ]}}]})


def test_local_uses_native_tools_instead_of_route_json():
    def handler(request):
        body = json.loads(request.content)
        assert "response_format" not in body
        assert body["tool_choice"] == "required"
        assert body["parallel_tool_calls"] is False
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        names = {tool["function"]["name"] for tool in body["tools"]}
        assert {"memory_reset", "manual_search", "conversation_offer", "vehicle_get_status"} <= names
        return response("media_play", {})
    result = client_for(handler).classify("sao không bật nhạc lên đi", [])
    assert result.intent == "media.play" and result.arguments.media_query is None


@pytest.mark.asyncio
async def test_async_native_offer_keeps_proposal_for_dialogue_manager():
    classifier = client_for(lambda _: response("conversation_offer", {
        "text": "Mình ở đây với bạn. Bạn muốn nghe nhạc không?",
        "proposal": {"intent": "media.play", "arguments": {}},
    }))
    result = await classifier.aclassify("Hôm nay thật chán", [])
    assert result.route == "conversation" and result.follow_up.intent == "media.play"


@pytest.mark.parametrize("name,arguments", [
    ("erase_everything", {}),
    ("vehicle_get_status", {"locked": True}),
    ("window_set_position", {"position_percent": True, "zone": "driver"}),
    ("conversation_reply", {"text": ""}),
])
def test_native_boundary_rejects_invalid_or_unknown_function(name, arguments):
    with pytest.raises(ValueError):
        decision_from_call(name, arguments)


def test_multiple_calls_cannot_escape_sequential_task_policy():
    classifier = client_for(lambda _: httpx.Response(200, json={"choices": [{"message": {
        "tool_calls": [{"function": {"name": "media_play", "arguments": "{}"}}] * 2
    }}]}))
    with pytest.raises(ValueError, match="exactly one"):
        classifier.classify("bật nhạc và mở cửa", [])


def test_local_timeout_has_one_attempt():
    attempts = []
    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("slow model", request=request)
    with pytest.raises(httpx.ReadTimeout):
        client_for(handler).classify("chào", [])
    assert len(attempts) == 1


@pytest.mark.parametrize("text", ["đừng mở nhạc", "không mở cửa sổ", "có"])
def test_prohibition_and_unbound_consent_do_not_reach_the_model(text):
    def forbidden(_):
        raise AssertionError("Must not call model or a tool")
    result = client_for(forbidden).classify(text, [])
    assert result.route in {"conversation", "clarify"}


def test_inferred_music_is_an_offer_not_an_action():
    result = client_for(lambda _: response("media_play", {})).classify("hôm nay buồn quá", [])
    assert result.route == "conversation"
    assert result.follow_up.intent == "media.play"


def test_explicit_stop_does_not_wait_for_model():
    def forbidden(_):
        raise AssertionError("Stop must bypass the model")
    assert client_for(forbidden).classify("Dừng nhạc", []).intent == "media.pause"


def test_model_cannot_invent_a_missing_zone():
    result = client_for(lambda _: response("window_set_position", {
        "zone": "driver", "position_percent": 50,
    })).classify("mở cửa sổ 50%", [])
    assert result.route == "clarify"
    assert result.follow_up.missing_slot == "zone"
    assert result.follow_up.arguments.zone is None
    assert result.follow_up.arguments.position_percent == 50


def test_model_cannot_turn_casual_chat_into_a_memory_write():
    result = client_for(lambda _: response("memory_remember", {
        "memory_key": "preferred_music", "memory_value": "jazz",
    })).classify("Tôi thích jazz", [])
    assert result.intent == "conversation.respond"
