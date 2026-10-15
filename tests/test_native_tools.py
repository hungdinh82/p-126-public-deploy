"""Local native transport proposes semantic frames, never executable tools."""
import json

import httpx
import pytest
from pydantic import ValidationError

from src.vivi.dialogue.contracts import SemanticTurn, SessionState
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.structured_llm import StructuredChatClient


def client_for(handler):
    return StructuredChatClient(base_url="http://local.test/v1", api_key="", model="test-model",
        timeout_seconds=1, max_attempts=1, transport=httpx.MockTransport(handler),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}})


def response(frame, name="interpret_turn", calls=1):
    if name == 'interpret_turn':
        act = frame['act']
        if act in {'reply', 'clarify', 'unsupported'}:
            name, frame = 'dialogue_' + act, {'text': frame.get('text') or 'Mình giữ nguyên.'}
        elif act == 'offer':
            name, frame = 'dialogue_suggest_unrequested_action', {key: value for key, value in frame.items() if key != 'act'}
        else:
            action = frame['actions'][0]
            name, frame = action['capability'].replace('.', '_'), action.get('arguments', {})
    return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
        {"function": {"name": name, "arguments": json.dumps(frame)}}] * calls}}]})


async def resolve(handler, text="bật nhạc", catalog=None):
    return await SemanticResolver({"local": ModelProvider("local", client_for(handler), 1)}).resolve(
        text, SessionState(), requested="local", catalog=catalog)


@pytest.mark.asyncio
async def test_local_uses_documented_native_proposals():
    def handler(request):
        body = json.loads(request.content)
        assert "response_format" not in body
        assert body["tool_choice"] == "required" and body["parallel_tool_calls"] is True
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        names = [t['function']['name'] for t in body['tools']]
        assert 'media_play' in names and 'trunk_set_open' not in names
        assert 'interpret_turn' not in names
        return response({"act":"request", "actions":[{"capability":"media.play"}]})
    frame, tier, _ = await resolve(handler)
    assert tier == "local" and frame.actions[0].capability == "media.play"


@pytest.mark.asyncio
async def test_explicitly_empty_catalog_does_not_enable_default_actions():
    def handler(request):
        assert all(item['function']['name'].startswith('dialogue_') for item in json.loads(request.content)['tools'])
        return response({'act': 'reply', 'text': 'Mình nghe bạn.'})
    frame, tier, _ = await resolve(handler, 'Xin chào', catalog=[])
    assert tier == 'local' and not frame.actions


@pytest.mark.asyncio
async def test_native_offer_keeps_proposal_for_reducer():
    def handler(request):
        names = [item['function']['name'] for item in json.loads(request.content)['tools']]
        if 'dialogue_suggest_unrequested_action' not in names:
            return response({'query': 'gợi ý phát nhạc'}, name='dialogue_find_tools')
        return response({"act":"offer", "text":"Bạn muốn nghe nhạc không?", "actions":[{"capability":"media.play"}]})
    frame, tier, _ = await resolve(handler, "Hôm nay thật chán")
    assert tier == "local" and frame.act == "offer" and frame.actions[0].capability == "media.play"


@pytest.mark.parametrize("frame", [
    {"act":"request", "actions":[{"capability":"vehicle.get_status", "arguments":{"locked":True}}]},
    {"act":"request", "actions":[{"capability":"window.set_position", "arguments":{"position_percent":True, "zone":"driver"}}]},
    {"act":"confirm", "actions":[{"capability":"media.play"}]},
    {"act":"clarify", "actions":[{"capability":"door.set_open"}]},
])
def test_semantic_boundary_rejects_invalid_parameters_and_contradictory_act(frame):
    with pytest.raises(ValidationError):
        SemanticTurn.model_validate(frame)


@pytest.mark.asyncio
async def test_unoffered_native_calls_repair_once_then_fall_back():
    attempts = []
    def handler(request):
        attempts.append(request)
        return response({}, name='not_an_offered_tool')
    frame, tier, trace = await resolve(handler, "bật nhạc và mở cửa")
    assert len(attempts) == 2 and tier == "rules" and trace[0]["reason"] == "ValueError"
    assert len(frame.actions) == 2  # Parser still requires a door slot, so no free execution.


@pytest.mark.asyncio
async def test_local_timeout_has_one_attempt_and_parser_fallback():
    attempts = []
    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("slow model", request=request)
    frame, tier, _ = await resolve(handler, "chào")
    assert len(attempts) == 1 and tier == "rules" and frame.act == "reply"


@pytest.mark.parametrize("text", ["đừng mở nhạc", "không mở cửa sổ", "có"])
@pytest.mark.asyncio
async def test_valid_model_negation_or_unbound_consent_never_falls_back(text):
    frame, tier, trace = await resolve(lambda _: response({"act":"reply" if text != "có" else "clarify", "negated": text != "có", "text":"Mình giữ nguyên."}), text)
    assert tier == "local" and not frame.actions and len(trace) == 1


@pytest.mark.asyncio
async def test_explicit_stop_uses_same_model_contract():
    frame, tier, _ = await resolve(lambda _: response({"act":"request", "actions":[{"capability":"media.pause"}]}), "Dừng nhạc")
    assert tier == "local" and frame.actions[0].capability == "media.pause"


@pytest.mark.asyncio
async def test_known_missing_zone_is_retained_for_reducer():
    frame, tier, _ = await resolve(lambda _: response({"act":"request", "actions":[{"capability":"window.set_position", "arguments":{"position_percent":50}}]}), "mở cửa sổ 50%")
    assert tier == "local" and frame.actions[0].arguments.zone is None


@pytest.mark.asyncio
async def test_casual_personal_statement_is_reply_without_memory_write():
    frame, tier, _ = await resolve(lambda _: response({"act":"reply", "text":"Mình nghe bạn rồi."}), "Tôi thích jazz")
    assert tier == "local" and frame.act == "reply" and not frame.actions
