"""Opt-in hardware regression: real Qwen, isolated checkpoints and simulated car.

VIVI_TEST_JETSON_URL=http://100.123.27.125:8795 \
VIVI_TEST_FORM_REPORT=/tmp/jetson-forms.json .venv/bin/pytest -q -s tests/test_jetson_live_forms.py
"""
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from src.vivi.config import get_settings
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.rag.generator import LocalHandbookGenerator
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.test_long_term_memory import services as _services

services = _services
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not os.getenv('VIVI_TEST_JETSON_URL'), reason='Opt-in Jetson hardware test')]


async def test_real_qwen_pending_form_lifecycle(services, tmp_path):
    url = os.environ['VIVI_TEST_JETSON_URL'].rstrip('/')
    client = LocalHandbookGenerator(get_settings().model_copy(update={'local_llm_base_url': url + '/v1',
        'local_llm_model': os.getenv('VIVI_TEST_JETSON_MODEL', 'vivi-qwen3-1.7b'), 'llm_timeout_seconds': 180})).client
    client.max_tokens = 512
    client.max_attempts = 1
    store = RuntimeStore(tmp_path / 'runtime.sqlite3')
    gateway = VehicleActionGateway(services.action_gateway.vehicle, DurableConfirmations(store), ledger=store)
    runtime = replace(services, action_gateway=gateway, generation_provider='rules')
    resolver = SemanticResolver({'local': ModelProvider('local', client, 180)}, requested_budgets={'local': 180})
    graph = build_assistant_graph(runtime, ThreadedSqliteSaver(tmp_path / 'checkpoint.sqlite3'), store, resolver=resolver)
    graph.default_provider = 'local'
    report = {'url': url, 'scope': 'Real local semantic inference and graph; simulated vehicle; STT/TTS excluded', 'turns': []}
    report_path = Path(os.getenv('VIVI_TEST_FORM_REPORT', str(tmp_path / 'report.json')))
    calls = []
    complete = client._acomplete

    async def recorded(payload):
        response = await complete(payload)
        calls.append({'user_prompt': payload['messages'][-1]['content'], 'usage': response.json().get('usage'), 'tool_calls': client._tool_results(response)})
        return response

    client._acomplete = recorded

    async def speak(session, text):
        calls.clear()
        started = time.perf_counter()
        result = await graph.ainvoke({'session_id': session, 'turn_id': f'{session}-{len(report["turns"])}', 'input_text': text})
        output = result['output']
        report['turns'].append({'session': session, 'text': text, 'wall_ms': round((time.perf_counter() - started) * 1000, 2),
            'status': output['status'], 'response': output['response_text'], 'calls': list(calls),
            'semantic': output['diagnostics']['semantic'], 'attempts': output['diagnostics']['provider_attempts'],
            'steps': output['steps'], 'suspended': len(result['session']['suspended'])})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(session, text, report['turns'][-1]['wall_ms'], output['response_text'], flush=True)
        return result

    try:
        for repeat in range(3):
            session = f'form-{repeat}'
            first = await speak(session, 'mở cửa')
            assert first['steps'][0]['status'] == 'waiting_slot'
            assert not first['session']['active']['steps'][0]['draft']['arguments']['zone']
            second = await speak(session, 'mở cửa sau phải')
            assert second['output']['action_proposal']['arguments'] == {'zone': 'rear_right', 'open': True}
            assert second['output']['confirmation']
            await speak(session, 'ok')
            assert gateway.vehicle.state_for(session).door_states.rear_right.open
            cold = await speak(session, 'tôi lạnh quá')
            assert cold['output']['diagnostics']['semantic']['actions'][0]['arguments']['temperature_delta_celsius'] == 2
            assert not cold['session']['suspended']

            session = f'partial-{repeat}'
            partial = await speak(session, 'mở cửa xe bên phải')
            assert partial['steps'][0]['status'] == 'waiting_slot'
            filled = await speak(session, 'phía sau')
            assert filled['output']['action_proposal']['arguments'] == {'zone': 'rear_right', 'open': True}
            grant = filled['output']['confirmation']['confirmation_id']
            cold = await speak(session, 'tôi lạnh quá')
            assert cold['output']['diagnostics']['semantic']['actions'][0]['capability'] == 'climate.set_temperature'
            assert not cold['session']['suspended']
            assert gateway.resolve_confirmation(session, 'stale-ok', grant, 'approve')[1].status == 'denied'
            assert not gateway.vehicle.state_for(session).door_states.rear_right.open
        assert not any(a.get('semantic_guard') for row in report['turns'] for a in row['attempts'])
        report['passed'] = True
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        graph.close()
