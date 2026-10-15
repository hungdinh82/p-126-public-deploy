"""Cloud retains main's contract; local has a compact native contract."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from scripts.benchmark_jetson_planner import correct_frame
from src.vivi.api.routes.voice_inputs import TranscriptInput
from src.vivi.config import Settings
from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.local_semantics import local_system_instruction
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver, semantic_tools
from src.vivi.domain.models import TurnRequest
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.speech.input_service import VoiceTurnRequest
from src.vivi.turn_control import current_control

MAIN = json.loads((Path(__file__).parent / 'fixtures/semantic_main_9c76585.json').read_text())


@pytest.mark.parametrize('provider', ['local', 'google'])
@pytest.mark.asyncio
async def test_main_prompt_and_complete_history_reach_both_providers(provider):
    captured = []

    class Model:
        async def agenerate_tool(self, **kwargs):
            captured.append(kwargs)
            return 'interpret_turn', {'act': 'reply', 'text': 'Mình nghe bạn.'}

        async def agenerate_json(self, **kwargs):
            captured.append(kwargs)
            return json.dumps({'act': 'reply', 'text': 'Mình nghe bạn.'})

    history = [{'query': f'{i} ' + 'q' * 800, 'answer': 'a' * 1000} for i in range(6)]
    resolver = SemanticResolver({provider: ModelProvider(provider, Model(), 1)})
    _, served, _ = await resolver.resolve('Xin chào', SessionState(), requested=provider, history=history)
    assert served == provider
    prompt = json.loads(captured[0]['user'])
    assert prompt['utterance'] == 'Xin chào'
    if provider == 'local':
        assert captured[0]['system'] == local_system_instruction()
        assert captured[0]['tools'] != MAIN['semantic_tools']
        assert len(prompt['recent_turns']) == 3
        assert len(prompt['recent_turns'][0]['query']) == 500
    else:
        assert captured[0]['system'] == MAIN['system']
        assert list(prompt) == ['utterance', 'tasks', 'conversation_context', 'recent_turns',
                                'explicit_user_memory', 'capabilities']
        assert prompt['recent_turns'] == history
    assert semantic_tools() == MAIN['semantic_tools']


@pytest.mark.asyncio
async def test_local_deadline_does_not_change_cloud_semantic_deadline():
    class Model:
        async def agenerate_json(self, **kwargs):
            await asyncio.sleep(.04)
            return json.dumps({'act': 'reply', 'text': 'Mình nghe bạn.'})

    for requested, served in [('google', 'rules'), ('local', 'local')]:
        resolver = SemanticResolver({requested: ModelProvider(requested, Model(), 1)},
                                    budget_seconds=.01, requested_budgets={'local': .2},
                                    fallback_chains={requested: [requested]})
        _, actual, _ = await resolver.resolve('Xin chào', SessionState(), requested=requested)
        assert actual == served


@pytest.mark.asyncio
async def test_long_local_turn_timeout_does_not_extend_cloud_turns():
    orchestrator = LangGraphOrchestrator({}, None, SimpleNamespace(), timeout_seconds=1,
                                        local_timeout_seconds=30)
    request = TurnRequest(transcript='Xin chào', session_id='budget', turn_id='one')
    for provider, expected in [('google', 1), ('local', 30)]:
        async with orchestrator.turn_scope(request, provider):
            assert expected - .2 < current_control.get().deadline - time.monotonic() <= expected


def test_durable_intake_is_opt_in_local_only_and_main_turn_limit_is_preserved():
    assert Settings().local_voice_buffering_enabled is False
    with pytest.raises(ValidationError):
        TranscriptInput(session_id='one', turn_id='one', transcript='Xin chào', llm_provider='google')
    with pytest.raises(ValidationError):
        TurnRequest(session_id='one', turn_id='one', transcript='x' * 501)
    assert len(VoiceTurnRequest(session_id='one', turn_id='one', transcript='x' * 2000).transcript) == 2000


def test_benchmark_does_not_count_guard_repair_as_model_correctness():
    from src.vivi.dialogue.contracts import SemanticTurn
    from src.vivi.dialogue.parser import OfflineParser

    raw = SemanticTurn.model_validate({'act': 'request', 'actions': [{
        'capability': 'climate.set_temperature', 'arguments': {'value_celsius': 27},
    }]})
    final = OfflineParser().guard('đặt nhiệt độ 24 độ', raw, SessionState())
    assert correct_frame('temperature', raw) is False
    assert correct_frame('temperature', final) is True
    assert correct_frame('temperature', None) is False


@pytest.mark.parametrize('provider', ['local', 'google'])
@pytest.mark.asyncio
async def test_answer_history_is_compact_only_for_actual_local_provider(provider):
    from src.vivi.conversation.service import ConversationService

    captured = []
    class Client:
        async def agenerate_json(self, **kwargs):
            captured.append(kwargs)
            return json.dumps({'text': 'Lá cây có màu xanh nhờ diệp lục.', 'supports': []})

    history = [{'query': 'q' * 800, 'answer': 'a' * 1000} for _ in range(6)]
    service = ConversationService({provider: ModelProvider(provider, Client(), 1)})
    result = await service.respond('Vì sao lá cây có màu xanh?', provider=provider, history=history)
    assert result['status'] == 'completed'
    payload = json.loads(captured[0]['user'])
    assert payload['question'] == 'Vì sao lá cây có màu xanh?'
    if provider == 'local':
        assert list(payload)[-1] == 'question'
        assert len(payload['recent_turns']) == 3
    else:
        assert list(payload)[0] == 'question'
        assert payload['recent_turns'] == history
