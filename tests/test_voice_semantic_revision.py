"""Pending speech is one grounded plan; history cannot supply new effects."""
import json

import httpx
import pytest

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver, native_semantic_prompt
from src.vivi.structured_llm import StructuredChatClient


@pytest.mark.parametrize('text,expected', [
    ('đặt nhiệt độ 24 độ\nà không, đặt 26 độ\nmở cửa kính bên phụ',
     [('climate.set_temperature', {'value_celsius': 26.0}),
      ('window.set_position', {'position_percent': 100, 'zone': 'front_passenger'})]),
    ('mở cửa kính bên tài\nà không, bên phụ',
     [('window.set_position', {'position_percent': 100, 'zone': 'front_passenger'})]),
    ('mở cửa kính bên tài\nmở cửa kính bên tài\nđặt nhiệt độ 23 độ',
     [('window.set_position', {'position_percent': 100, 'zone': 'driver'}),
      ('climate.set_temperature', {'value_celsius': 23.0})]),
])
def test_offline_fallback_snapshot_keeps_additions_and_only_corrected_effect(text, expected):
    parser = OfflineParser()
    frame = parser.interpret(text, SessionState())
    assert [(a.capability, a.arguments.model_dump(exclude_none=True)) for a in frame.actions] == expected


def test_weather_correction_keeps_place_and_replaces_date():
    frame = OfflineParser().interpret('thời tiết Hà Nội hôm nay\ný tôi là ngày mai', SessionState())
    assert len(frame.actions) == 1
    assert frame.actions[0].arguments.location == 'Hà Nội'
    assert frame.actions[0].arguments.forecast_day == 'tomorrow'


@pytest.mark.parametrize('act', ['request', 'question'])
def test_current_query_replaces_old_topic_without_overriding_model_routing(act):
    proposal = SemanticTurn(act=act, actions=[ActionDraft(
        capability='conversation.respond', arguments={'query':'Một cục xe ở đâu?'})])
    frame = OfflineParser().guard('Tôi lạnh quá!', proposal, SessionState())
    assert frame.act == act
    assert len(frame.actions) == 1
    assert frame.actions[0].capability == 'conversation.respond'
    assert frame.actions[0].arguments.query == 'Tôi lạnh quá!'


def test_committed_climate_continuation_works_without_replaying_completed_task():
    session = SessionState(active=DialogueTask(steps=[TaskStep(status='completed', draft=ActionDraft(
        capability='climate.set_temperature', arguments={'value_celsius':26}))]))
    assert SemanticResolver.context(session)['active'] is None
    frame = OfflineParser().guard('tăng tiếp', SemanticTurn(act='clarify', text='Tăng gì?'), session)
    assert frame.act == 'request'
    assert frame.actions[0].arguments.temperature_delta_celsius == 2


@pytest.mark.parametrize('text', [
    'Mở gửi xe xa cho tôi',
    'Mở gửi xe xa cho tôi\nMở gửi xe xa cho tôi',
    'mở cửa kính bên tài\nmở gửi xe xa cho tôi',
])
def test_unknown_offline_grammar_does_not_reject_valid_model_action(text):
    proposal = SemanticTurn(act='request', actions=[ActionDraft(
        capability='vehicle.get_status', arguments={'query': 'summary'})])
    frame = OfflineParser().guard(text, proposal, SessionState())
    assert frame.act == 'request'
    assert frame.actions == proposal.actions
    fallback = OfflineParser().interpret(text, SessionState())
    assert fallback.act == 'clarify'


@pytest.mark.parametrize('text', [
    'tăng nhiệt độ điều hòa trong cabin lên một chút',
    'chỉnh máy lạnh tới hai mươi lăm độ',
    'tăng nhiệt độ điều hòa trong cabin lên một chút\nchỉnh thành hai mươi lăm độ',
])
@pytest.mark.asyncio
async def test_slm_temperature_action_survives_unrecognized_offline_grammar(text):
    proposal = {'act': 'request', 'actions': [{'capability': 'climate.set_temperature',
                                             'arguments': {'value_celsius': 25}}]}

    class Model:
        async def agenerate_json(self, **kwargs):
            assert json.loads(kwargs['user'])['utterance'] == text
            return json.dumps(proposal)

    assert OfflineParser().interpret(text, SessionState()).act == 'clarify'
    resolver = SemanticResolver({'local': ModelProvider('local', Model(), 1)})
    frame, served, attempts = await resolver.resolve(text, SessionState(), requested='local')
    assert served == 'local'
    assert frame == SemanticTurn.model_validate(proposal)
    assert not attempts[-1].get('semantic_guard')


@pytest.mark.asyncio
async def test_old_question_is_not_forwarded_as_current_conversation_query():
    old = 'Một cục nính xe ở đâu rồi?'
    current = 'Tô lạnh quá!'
    session = SessionState(active=DialogueTask(steps=[TaskStep(status='completed', draft=ActionDraft(
        capability='conversation.respond', arguments={'query': old}))]))

    def backend(request):
        payload = json.loads(request.content)
        prompt = json.loads(payload['messages'][-1]['content'])
        assert prompt['tasks']['active'] is None
        assert prompt['utterance'] == current
        return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{
            'function': {'name': 'interpret_turn', 'arguments': json.dumps({
                'act': 'request', 'actions': [{'capability': 'conversation.respond', 'arguments': {'query': old}}],
            })},
        }]}}]})

    client = StructuredChatClient(base_url='http://test/v1', api_key='', model='test',
                                 transport=httpx.MockTransport(backend), max_attempts=1, timeout_seconds=5)
    resolver = SemanticResolver({'local': ModelProvider('local', client, 5)})
    frame, _, _ = await resolver.resolve(current, session, requested='local',
                                         history=[{'query': old, 'answer': 'Câu trả lời cũ'}],
                                         catalog=catalog(False))
    assert all(a.arguments.query != old for a in frame.actions)


def test_current_utterance_is_after_history_in_native_prompt():
    prompt = native_semantic_prompt({'utterance': 'câu mới', 'recent_turns': ['câu cũ'], 'capabilities': []})
    assert list(json.loads(prompt))[-1] == 'utterance'
