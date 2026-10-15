"""Tool discovery and proposal conversion do not execute or silently authorize."""
import json

import pytest
from jsonschema import Draft202012Validator

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.local_tools import (
    local_tool_documents,
    native_proposal_prompt,
    native_proposal_tools,
    proposals_to_turn,
)
from src.vivi.dialogue.revisions import coalesce_explicit_revisions
from src.vivi.dialogue.tool_discovery import MAX_TOOL_DOCUMENTS, discover_capabilities, initial_tool_documents


@pytest.mark.parametrize('navigation', [False, True])
def test_mcp_shaped_documents_are_self_contained(navigation):
    documents = local_tool_documents(catalog(navigation))
    assert len({item['name'] for item in documents}) == len(documents)
    for item in documents:
        assert item['description'] and item['_meta']['proposalOnly']
        Draft202012Validator.check_schema(item['inputSchema'])
        # Resolve refs as well as checking syntax.
        if item['_meta'].get('capability') in {'navigation.plan', 'trip.plan'}:
            Draft202012Validator(item['inputSchema']).validate({'query': 'Hà Nội'})


@pytest.mark.parametrize('text,names', [
    ('mở cốp xe', {'trunk.set_open'}),
    ('mở cửa kính bên tài', {'window.set_position'}),
    ('mở cửa kính bên tài và đóng cửa bên phụ', {'window.set_position', 'door.set_open'}),
    ('đặt nhiệt độ 24 độ\nà không đặt 26 độ\nmở cửa kính bên phụ', {'climate.set_temperature', 'window.set_position'}),
])
def test_discovery_selects_all_positive_mentions_not_generic_substrings(text, names):
    assert {item['name'] for item in discover_capabilities(text, catalog(False))} == names


def test_unrecognized_vocabulary_retains_all_tools_and_allows_discovery():
    caps = catalog(False)
    assert discover_capabilities('câu mới chưa có từ vựng', caps) == caps
    names = {item['function']['name'] for item in native_proposal_tools(caps)}
    assert 'dialogue_find_tools' in names
    assert 'dialogue_suggest_unrequested_action' not in names
    selected, index = initial_tool_documents('câu mới chưa có từ vựng', catalog(True))
    assert len(selected) <= MAX_TOOL_DOCUMENTS
    assert {item['name'] for item in index} == {item['name'] for item in catalog(True)}
    assert native_proposal_tools([])


def test_schema_binds_only_unambiguous_spoken_target():
    caps = discover_capabilities('mở cửa kính bên tài', catalog(False))
    tool = next(item for item in native_proposal_tools(caps, utterance='mở cửa kính bên tài')
                if item['function']['name'] == 'window_set_position')
    validator = Draft202012Validator(tool['function']['parameters'])
    validator.validate({'zone': 'driver', 'position_percent': 100})
    assert list(validator.iter_errors({'position_percent': 100}))
    assert list(validator.iter_errors({'zone': 'front_passenger', 'position_percent': 100}))
    for text in ['mở cửa kính trừ bên tài', 'mở kính bên tài và bên phụ']:
        tool = next(item for item in native_proposal_tools(caps, utterance=text)
                    if item['function']['name'] == 'window_set_position')
        assert 'oneOf' in tool['function']['parameters']


def test_ordered_multi_tool_proposals_are_atomic_and_only_normalize_equivalent_slots():
    calls = [('climate_set_temperature', {'value_celsius': 26}),
             ('window_set_position', {'zones': ['front_passenger'], 'position_percent': 100})]
    frame = proposals_to_turn(calls, catalog(False))
    assert frame.act == 'request'
    assert [action.capability for action in frame.actions] == ['climate.set_temperature', 'window.set_position']
    assert frame.actions[1].arguments.model_dump(exclude_none=True) == {'zone': 'front_passenger', 'position_percent': 100}
    with pytest.raises(ValueError):
        proposals_to_turn([*calls, ('trunk_set_open', {'gear': 'D'})], catalog(False))


@pytest.mark.parametrize('direction,delta', [('increase', 2), ('decrease', -2)])
def test_comfort_tools_have_explicit_direction_and_server_default(direction, delta):
    frame = proposals_to_turn([('climate_adjust_temperature', {'direction': direction})], catalog(False))
    assert frame.actions[0].arguments.model_dump(exclude_none=True) == {'temperature_delta_celsius': delta}
    with pytest.raises(ValueError):
        proposals_to_turn([('climate_adjust_temperature', {'direction': direction, 'amount': -2})], catalog(False))


def test_unoffered_tools_and_offer_overrides_are_rejected():
    selected = discover_capabilities('mở cốp', catalog(False))
    for calls in [[('window_set_position', {'position_percent': 100})],
                  [('dialogue_suggest_unrequested_action', {'text': 'Mở nhé?', 'actions': []})]]:
        with pytest.raises(ValueError):
            proposals_to_turn(calls, selected)
    with pytest.raises(ValueError):
        proposals_to_turn([('dialogue_suggest_unrequested_action', {
            'act': 'request', 'text': 'Mở nhé?', 'actions': [{'capability': 'trunk.set_open', 'arguments': {'open': True}}]})],
            selected, include_suggestions=True)


def test_revision_projection_preserves_independent_and_unknown_requests():
    original = 'đặt nhiệt độ 24 độ\nà không, đặt 26 độ\nmở cửa kính bên phụ'
    assert coalesce_explicit_revisions(original) == 'đặt nhiệt độ 26 độ\nmở cửa kính bên phụ'
    for text in ['đặt nhiệt độ 24 độ\nmở cốp', 'mở cốp và cửa kính\nà không đóng cốp',
                 'đặt nhiệt độ 24 độ\nà không đừng đặt 26 độ',
                 'đặt nhiệt độ 24 độ\nà không đặt 90 độ', 'mở cốp\nà không nói lại giúp tôi']:
        assert coalesce_explicit_revisions(text) == text
    assert original.startswith('đặt nhiệt độ 24 độ')  # No mutation of source transcript.


def test_current_speech_boundaries_and_real_context_are_retained():
    value = {'utterance': 'câu một\ncâu hai', 'recent_turns': [], 'tasks': {}, 'conversation_context': {}}
    assert native_proposal_prompt(value).endswith(value['utterance'])
    assert 'YÊU CẦU HIỆN TẠI' in native_proposal_prompt(value)
    value['recent_turns'] = [{'query': 'câu trước', 'answer': 'đã xong'}]
    rendered = native_proposal_prompt(value)
    assert rendered.endswith('câu một\ncâu hai')
    assert json.dumps(value['recent_turns'], ensure_ascii=False, separators=(',', ':')) in rendered


def test_public_actions_have_executable_documentation_including_music_and_navigation():
    import re
    from pathlib import Path

    from src.vivi.agents.tools import TOOL_REGISTRY

    documented = {item['_meta'].get('capability') for item in local_tool_documents(catalog(True))}
    assert documented - {None} == set(TOOL_REGISTRY)
    public_actions = set()
    root = Path(__file__).resolve().parents[1]
    for filename in ('app.js', 'journey.js'):
        for expression in re.findall(r'capability\s*:\s*([^}\n]+)', (root / filename).read_text()):
            public_actions.update(re.findall(r"['\"]([a-z]+\.[a-z_]+)['\"]", expression))
    assert {'media.play', 'media.pause', 'door.set_lock', 'navigation.select'} <= public_actions
    assert public_actions <= documented
    for name in ('media.play', 'media.pause'):
        docs = next(item for item in local_tool_documents(catalog(True)) if item['_meta'].get('capability') == name)
        assert len(docs['description']) > 100


@pytest.mark.parametrize('text,capability,args', [
    ('bật nhạc', 'media.play', {}),
    ('phát nhạc thư giãn', 'media.play', {'media_query': 'thư giãn'}),
    ('tạm dừng nhạc', 'media.pause', {}),
])
def test_music_proposals_match_public_backend_contract(text, capability, args):
    caps = discover_capabilities(text, catalog(False))
    assert capability in {item['name'] for item in caps}
    frame = proposals_to_turn([(capability.replace('.', '_'), args)], caps, utterance=text)
    assert frame.actions[0].capability == capability
    assert frame.actions[0].arguments.model_dump(exclude_none=True) == args


def test_missing_absolute_target_rejects_fabrication_instead_of_correcting_the_action():
    with pytest.raises(ValueError, match='No absolute temperature'):
        proposals_to_turn([('climate_set_temperature', {'value_celsius': 23})], catalog(False), utterance='tăng nhiệt độ điều hòa lên')
    frame = proposals_to_turn([('climate_set_temperature', {'value_celsius': 24})], catalog(False), utterance='đặt nhiệt độ 24 độ')
    assert frame.actions[0].arguments.value_celsius == 24
