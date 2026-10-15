"""Lifecycle, identity and consent checks are independent of model accuracy."""
import pytest
from jsonschema import Draft202012Validator

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SessionState, TaskStep
from src.vivi.dialogue.form_tools import current_form, decode_form_call, lifecycle_tools
from src.vivi.dialogue.local_tools import local_tool_documents
from src.vivi.dialogue.planner import local_context


def prepared(status='waiting_confirmation'):
    session = SessionState(active=DialogueTask(steps=[TaskStep(status=status,
        draft=ActionDraft(capability='door.set_open', arguments={'open': True, 'zone': 'rear_right'}),
        resolved_arguments={'open': True, 'zone': 'rear_right'})]))
    docs = local_tool_documents(catalog(False))
    tasks = local_context(session)
    form = current_form(tasks, docs)
    payload = {k: form[k] for k in ('form_id', 'revision', 'values')}
    return session, docs, tasks, payload


def test_generated_form_schemas_are_self_contained():
    _, docs, tasks, _ = prepared()
    for tool in lifecycle_tools(docs, tasks):
        Draft202012Validator.check_schema(tool['function']['parameters'])


def test_submit_requires_consent_and_all_reviewed_values():
    _, docs, tasks, payload = prepared()
    with pytest.raises(ValueError):
        decode_form_call([('form_submit', payload)], docs, tasks)
    wrong = {**payload, 'values': {'open': True, 'zone': 'driver'}}
    with pytest.raises(ValueError):
        decode_form_call([('form_submit', wrong)], docs, tasks, allow_submit=True)
    frame = decode_form_call([('form_submit', payload)], docs, tasks, allow_submit=True)
    assert frame.act == 'confirm' and frame.task_ref == 'active'


def test_stale_revision_and_replacement_form_cannot_be_submitted():
    _, docs, tasks, payload = prepared()
    stale = {**payload, 'revision': payload['revision'] - 1}
    with pytest.raises(ValueError):
        decode_form_call([('form_submit', stale)], docs, tasks, allow_submit=True)
    _, _, replacement, _ = prepared()
    with pytest.raises(ValueError):
        decode_form_call([('form_submit', payload)], docs, replacement, allow_submit=True)


def test_incomplete_or_closed_form_cannot_submit_and_update_is_atomic():
    session, docs, tasks, payload = prepared('waiting_slot')
    with pytest.raises(ValueError):
        decode_form_call([('form_submit', payload)], docs, tasks, allow_submit=True)
    with pytest.raises(ValueError):
        decode_form_call([('form_update', payload), ('form_update', payload)], docs, tasks)
    session.active.steps[0].status = 'completed'
    assert current_form(local_context(session), docs) is None


def test_update_must_add_or_change_information():
    _, docs, tasks, payload = prepared('waiting_slot')
    unchanged = {**payload, 'values': {'zone': 'rear_right'}}
    with pytest.raises(ValueError, match='No form values changed'):
        decode_form_call([('form_update', unchanged)], docs, tasks)


@pytest.mark.asyncio
async def test_bad_model_submit_cannot_execute_through_rules_fallback(services, tmp_path):
    from tests.test_dialogue_v2 import build, invoke

    class Model:
        async def agenerate_tools(self, **kwargs):
            props = next((t['function']['parameters']['properties'] for t in kwargs['tools']
                          if t['function']['name'] == 'form_submit'), None)
            if props:
                return [('form_submit', {'form_id': props['form_id']['enum'][0],
                    'revision': props['revision']['enum'][0], 'values': {'open': True, 'zone': 'driver'}})]
            update = next((t['function']['parameters']['properties'] for t in kwargs['tools']
                           if t['function']['name'] == 'form_update'), None)
            if update:
                return [('form_update', {'form_id': update['form_id']['enum'][0],
                    'revision': update['revision']['enum'][0], 'values': {'zone': 'rear_right'}})]
            return [('form_create', {'operation': 'door_open', 'values': {'zone': None}})]

    graph, _, gateway = build(services, tmp_path, local=Model())
    graph.default_provider = 'local'
    try:
        await invoke(graph, 'mở cửa', 'create')
        await invoke(graph, 'sau phải', 'update')
        result = await invoke(graph, 'ok', 'bad-submit')
        assert result['output']['status'] == 'clarify'
        assert result['session']['active']['steps'][0]['status'] == 'waiting_confirmation'
        assert all(not door['open'] for door in gateway.vehicle.state_for('driver').door_states.model_dump().values())
    finally:
        graph.close()


from tests.test_long_term_memory import services as services  # noqa: E402


def test_compound_form_creation_is_ordered_and_all_or_nothing():
    docs = local_tool_documents(catalog(False))
    calls = [('form_create', {'operation': 'trunk_open', 'values': {}}),
             ('form_create', {'operation': 'window_set_position', 'values': {'zone': 'driver', 'position_percent': 100}})]
    result = decode_form_call(calls, docs, {})
    assert [name for _, name, _ in result] == ['trunk_set_open', 'window_set_position']
    with pytest.raises(ValueError):
        decode_form_call([*calls, ('form_create', {'operation': 'unknown', 'values': {}})], docs, {})


@pytest.mark.parametrize('opening', [True, False])
def test_location_collection_cannot_flip_reviewed_operation(opening):
    session, docs, _, _ = prepared('waiting_slot')
    session.active.steps[0].draft.arguments.open = opening
    session.active.steps[0].resolved_arguments = {'open': opening, 'zone': 'rear_right'}
    tasks = local_context(session)
    form = current_form(tasks, docs)
    assert form['operation'] == ('door_open' if opening else 'door_close')
    payload = {'form_id': form['form_id'], 'revision': form['revision'], 'values': {'open': not opening}}
    with pytest.raises(ValueError):
        decode_form_call([('form_update', payload)], docs, tasks)


def test_model_close_operation_maps_to_false_without_parsing_utterance():
    docs = local_tool_documents(catalog(False))
    decoded = decode_form_call([('form_create', {'operation': 'door_close', 'values': {'zone': 'all'}})], docs, {})
    assert decoded == [('create', 'door_set_open', {'zone': 'all', 'open': False})]


def test_location_patch_cannot_mix_conflicting_representations():
    _, docs, tasks, payload = prepared('waiting_slot')
    conflicting = {**payload, 'values': {'zone': 'driver', 'target_side': 'right', 'target_row': 'rear'}}
    with pytest.raises(ValueError, match='never both'):
        decode_form_call([('form_update', conflicting)], docs, tasks)


@pytest.mark.asyncio
async def test_remaining_door_request_receives_fresh_state_and_closes_only_remaining(services, tmp_path):
    import json

    from tests.test_dialogue_v2 import build, invoke

    class Model:
        async def agenerate_tools(self, **kwargs):
            context = json.loads(kwargs['user'].splitlines()[1])
            form = context.get('form')
            if kwargs['user'].endswith('ok'):
                return [('form_submit', {k: form[k] for k in ('form_id', 'revision', 'values')})]
            observation = context['vehicle_observation']['door_open']
            if kwargs['user'].endswith('mở tất cả cửa'):
                assert not any(observation.values())
                return [('form_create', {'operation': 'door_open', 'values': {'zone': 'all'}})]
            if kwargs['user'].endswith('đóng cửa bên tài'):
                assert all(observation.values())
                return [('form_create', {'operation': 'door_close', 'values': {'zone': 'driver'}})]
            assert kwargs['user'].endswith('đóng hết 3 cửa còn lại')
            assert observation == {'driver': False, 'front_passenger': True, 'rear_left': True, 'rear_right': True}
            return [('form_create', {'operation': 'door_close',
                'values': {'zones': ['front_passenger', 'rear_left', 'rear_right']}})]

    graph, _, gateway = build(services, tmp_path, local=Model())
    graph.default_provider = 'local'
    try:
        for i, text in enumerate(['mở tất cả cửa', 'ok', 'đóng cửa bên tài', 'ok', 'đóng hết 3 cửa còn lại', 'ok']):
            result = await invoke(graph, text, f'door-{i}')
            if text.startswith('đóng'):
                assert result['output']['action_proposal']['arguments']['open'] is False
        assert result['output']['status'] == 'action_verified'
        assert all(not door['open'] for door in gateway.vehicle.state_for('driver').door_states.model_dump().values())
    finally:
        graph.close()
