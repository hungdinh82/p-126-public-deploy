"""The local wire schema allows concise output and prevents invalid tool shapes."""
import json

import pytest
from jsonschema import Draft202012Validator

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SessionState, TaskStep
from src.vivi.dialogue.local_semantics import local_semantic_tools
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver


@pytest.fixture(scope='module')
def validator():
    schema = local_semantic_tools(catalog(True))[0]['function']['parameters']
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize('frame', [
    {'act': 'request', 'actions': [{'capability': 'trunk.set_open', 'arguments': {'open': True}}]},
    {'act': 'request', 'actions': [{'capability': 'window.set_position', 'arguments': {'position_percent': 50}}]},
    {'act': 'request', 'actions': [{'capability': 'climate.set_temperature', 'arguments': {'temperature_delta_celsius': 2}}]},
    {'act': 'clarify', 'text': 'Bạn nói lại yêu cầu nhé.'},
    {'act': 'modify', 'task_ref': 'active', 'step_updates': [{'step_index': 0, 'action': {
        'capability': 'climate.set_temperature', 'arguments': {'value_celsius': 26}}}]},
])
def test_minimal_frames_and_missing_slots_are_allowed(validator, frame):
    validator.validate(frame)


@pytest.mark.parametrize('frame', [
    {'act': 'request', 'actions': [{'capability': 'trunk.set_open', 'arguments': {'open': True, 'query': 'summary'}}]},
    {'act': 'request', 'actions': [{'capability': 'window.set_position', 'arguments': {'open': True}}]},
    {'act': 'request', 'actions': [{'capability': 'not.a.tool'}]},
    {'act': 'request', 'actions': [], 'step_updates': []},
    {'act': 'request', 'actions': [{'capability': 'trunk.set_open'}], 'task_ref': 'active'},
    {'act': 'reply', 'text': 'OK', 'actions': [{'capability': 'trunk.set_open'}]},
    {'act': 'modify', 'task_ref': 'active', 'actions': [], 'step_updates': []},
])
def test_irrelevant_arguments_and_contradictory_act_fields_are_rejected(validator, frame):
    assert list(validator.iter_errors(frame))


@pytest.mark.asyncio
async def test_completed_task_not_exposed_as_local_active_work():
    captured = []
    class Client:
        async def agenerate_tool(self, **kwargs):
            captured.append(kwargs)
            return 'interpret_turn', {'act': 'request', 'actions': [{
                'capability': 'climate.set_temperature', 'arguments': {'temperature_delta_celsius': 2}}]}
    session = SessionState(active=DialogueTask(steps=[TaskStep(status='completed', draft=ActionDraft(
        capability='trunk.set_open', arguments={'open': True}))]))
    resolver = SemanticResolver({'local': ModelProvider('local', Client(), 1)})
    await resolver.resolve('Tôi lạnh quá', session, requested='local', catalog=catalog(False))
    assert json.loads(captured[0]['user'])['tasks']['active'] is None


@pytest.mark.asyncio
async def test_one_bad_local_answer_does_not_disable_following_requests():
    class Client:
        calls = 0
        async def agenerate_tool(self, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                return 'wrong_tool', {}
            return 'interpret_turn', {'act': 'reply', 'text': 'Mình nghe bạn.'}
    client = Client()
    resolver = SemanticResolver({'local': ModelProvider('local', client, 1)})
    _, served, _ = await resolver.resolve('chào', SessionState(), requested='local')
    assert served == 'rules'
    _, served, _ = await resolver.resolve('chào', SessionState(), requested='local')
    assert served == 'local'
