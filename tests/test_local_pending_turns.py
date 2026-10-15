"""Regressions for local turns following a clarification or partial target."""

import pytest

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.contracts import ActionDraft, ClarificationState, DialogueTask, SessionState, TaskStep
from src.vivi.dialogue.local_answers import resolve_location_component
from src.vivi.dialogue.local_tools import native_proposal_tools, proposals_to_turn
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver, local_context
from src.vivi.dialogue.reducer import reduce_turn
from src.vivi.dialogue.tool_discovery import discover_capabilities


def pending_door():
    return SessionState(active=DialogueTask(steps=[TaskStep(status='waiting_slot',
        draft=ActionDraft(capability='door.set_open', arguments={'open': True, 'target_side': 'right'}),
        missing_slots=[], result={'message': 'Bạn muốn phía trước hay phía sau?'})]))


@pytest.mark.asyncio
@pytest.mark.parametrize('session', [pending_door(), SessionState(clarification=ClarificationState(
    source_text='mở cửa sau phải', question='Bạn nói rõ lại yêu cầu nhé.'))])
async def test_pending_context_stays_on_native_contract_and_new_input_owns_query(session):
    captured = []

    class Model:
        async def agenerate_tools(self, **kwargs):
            captured.append(kwargs)
            return [('climate_adjust_temperature', {'direction': 'increase'})]

        async def agenerate_tool(self, **kwargs):
            pytest.fail('Pending state must not revert to the legacy DTO')

    resolver = SemanticResolver({'local': ModelProvider('local', Model(), 1)})
    frame, served, _ = await resolver.resolve('tôi lạnh quá', session, requested='local',
        history=[{'query': 'mở cửa sau phải', 'answer': 'Bạn nói rõ lại yêu cầu nhé.'}])
    assert served == 'local'
    assert frame.actions[0].arguments.temperature_delta_celsius == 2
    assert captured[0]['user'].endswith('tôi lạnh quá')
    assert all(item['function']['name'] != 'interpret_turn' for item in captured[0]['tools'])


def test_partial_row_completes_existing_side_without_replaying_old_topic():
    session = pending_door()
    task_id = session.active.id
    frame = resolve_location_component('phía sau', session)
    assert frame.act == 'modify'
    updated, _ = reduce_turn(session, frame)
    assert updated.active.id == task_id
    args = updated.active.steps[0].draft.arguments
    assert args.open is True and args.target_side == 'right' and args.target_row == 'rear'
    for text in ['tôi lạnh quá', 'mở cốp xe', 'mở cửa trước', 'phía sau và mở cốp']:
        assert resolve_location_component(text, session) is None


@pytest.mark.parametrize('text,args', [
    ('mở cửa', {'open': True}),
    ('mở cửa xe bên phải', {'open': True, 'target_side': 'right'}),
    ('mở cửa sau phải', {'open': True, 'zone': 'rear_right'}),
])
def test_target_schema_and_adapter_never_invent_a_missing_location(text, args):
    caps = discover_capabilities(text, catalog(False))
    frame = proposals_to_turn([('door_set_open', args)], caps, utterance=text)
    assert frame.actions[0].arguments.model_dump(exclude_none=True) == args
    with pytest.raises(ValueError):
        proposals_to_turn([('door_set_open', {'open': True, 'zone': 'roof'})], caps, utterance=text)


def test_current_conversation_query_is_host_bound_and_clarify_does_not_echo_stt():
    caps = catalog(False, conversation=True)
    tool = next(tool['function'] for tool in native_proposal_tools(caps) if tool['function']['name'] == 'conversation_respond')
    assert 'query' not in tool['parameters']['properties']
    frame = proposals_to_turn([('conversation_respond', {})], caps, utterance='Kể chuyện cho tôi')
    assert frame.actions[0].arguments.query == 'Kể chuyện cho tôi'
    with pytest.raises(ValueError):
        proposals_to_turn([('conversation_respond', {'query': 'Bạn nói rõ lại yêu cầu nhé.'})], caps,
                          utterance='Kể chuyện cho tôi')
    frame = proposals_to_turn([('dialogue_clarify', {'text': 'Một gần kì sao?'})], caps, utterance='Một gần kì sao?')
    assert frame.text != 'Một gần kì sao?'


def test_patch_tools_are_typed_bound_to_live_steps_and_atomic_with_added_actions():
    session = pending_door()
    tasks = local_context(session)
    caps = catalog(False)
    name = 'update_active_step_0__door_set_open'
    frame = proposals_to_turn([(name, {'target_row': 'rear'}), ('trunk_set_open', {'open': True})],
                              caps, tasks=tasks, utterance='phía sau, thêm mở cốp')
    assert frame.act == 'modify' and frame.task_ref == 'active'
    assert frame.step_updates[0].step_index == 0
    assert frame.append_actions[0].capability == 'trunk.set_open'
    session.active.steps[0].status = 'completed'
    with pytest.raises(ValueError):
        proposals_to_turn([(name, {'target_row': 'rear'})], caps, tasks=local_context(session))


@pytest.mark.asyncio
async def test_graph_form_collects_row_then_closes_on_new_topic_and_revokes_old_ok(services, tmp_path):
    from tests.test_dialogue_v2 import build, invoke

    class Model:
        async def agenerate_tools(self, **kwargs):
            if kwargs['user'].endswith('mở cửa xe bên phải'):
                return [('door_set_open', {'open': True, 'target_side': 'right'})]
            if kwargs['user'].endswith('tôi lạnh quá'):
                return [('climate_adjust_temperature', {'direction': 'increase'})]
            if kwargs['user'].endswith('phía sau'):
                return [('update_active_step_0__door_set_open', {'target_row': 'rear'})]
            return [('dialogue_reply', {'text': 'Mình nghe bạn.'})]

    graph, _, gateway = build(services, tmp_path, local=Model())
    graph.default_provider = 'local'
    try:
        first = await invoke(graph, 'mở cửa xe bên phải', 'form-open')
        assert first['steps'][0]['status'] == 'waiting_slot'
        second = await invoke(graph, 'phía sau', 'form-fill')
        assert second['output']['action_proposal']['arguments'] == {'zone': 'rear_right', 'open': True}
        grant = second['output']['confirmation']['confirmation_id']
        third = await invoke(graph, 'tôi lạnh quá', 'new-topic')
        assert not third['session']['suspended']
        assert third['session']['clarification'] is None
        assert third['session']['active']['steps'][0]['draft']['capability'] == 'climate.set_temperature'
        assert gateway.resolve_confirmation('driver', 'stale-ok', grant, 'approve')[1].status == 'denied'
        await invoke(graph, 'ok', 'unrelated-ok')
        assert not gateway.vehicle.state_for('driver').door_states.rear_right.open
    finally:
        graph.close()


from tests.test_long_term_memory import services as _services  # noqa: E402

services = _services


def test_filtered_suspended_form_keeps_its_original_task_handle():
    task = local_context(pending_door())['active']
    task['handle'] = 'suspended:1'
    frame = proposals_to_turn([('update_suspended_1_step_0__door_set_open', {'target_row': 'rear'})],
                              catalog(False), tasks={'suspended': [task]})
    assert frame.task_ref == 'suspended:1'


@pytest.mark.parametrize('text,zone', [
    ('sau phải', 'rear_right'), ('sau phai', 'rear_right'), ('sau phảii', 'rear_right'),
    ('sau trái', 'rear_left'), ('bên tài', 'driver'), ('bên tàii', 'driver'),
    ('phụ', 'front_passenger'), ('hành khách', 'front_passenger'),
])
@pytest.mark.asyncio
async def test_model_fills_location_form_without_lexical_override(services, tmp_path, monkeypatch, text, zone):
    from src.vivi.dialogue.parser import OfflineParser
    from tests.test_dialogue_v2 import build, invoke

    class Model:
        calls = 0

        async def agenerate_tools(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return [('form_create', {'operation': 'door_set_open', 'values': {'open': True}})]
            if self.calls == 3:
                assert kwargs['user'].endswith('ok')
                tool = next(t['function'] for t in kwargs['tools'] if t['function']['name'] == 'form_submit')
                props = tool['parameters']['properties']
                return [('form_submit', {'form_id': props['form_id']['enum'][0], 'revision': props['revision']['enum'][0],
                                         'values': {'open': True, 'zone': zone}})]
            assert kwargs['user'].endswith(text)
            assert 'field_schema' in kwargs['user']
            name = 'form_update'
            tool = next(t['function'] for t in kwargs['tools'] if t['function']['name'] == name)
            assert 'const' not in str(tool['parameters']['properties']['values'])
            props = tool['parameters']['properties']
            return [(name, {'form_id': props['form_id']['enum'][0], 'revision': props['revision']['enum'][0], 'values': {'zone': zone}})]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = 'local'
    try:
        first = await invoke(graph, 'mở cửa xe cho tôi', 'start-form')
        # A location grammar must not repair or replace the model proposal.
        def forbidden(*args, **kwargs):
            pytest.fail('Model-owned form must not call parser.guard')
        monkeypatch.setattr(OfflineParser, 'guard', forbidden)
        second = await invoke(graph, text, 'fill-form')
        assert model.calls == 2
        assert second['session']['active']['id'] == first['session']['active']['id']
        assert second['output']['action_proposal']['arguments'] == {'open': True, 'zone': zone}
        assert second['output']['confirmation']['confirmation_id']
        assert all(not door['open'] for door in gateway.vehicle.state_for('driver').door_states.model_dump().values())
        done = await invoke(graph, 'ok', 'confirm-form')
        assert done['output']['status'] == 'action_verified'
        assert model.calls == 3
        assert gateway.vehicle.state_for('driver').door_states.model_dump()[zone]['open']
    finally:
        graph.close()
