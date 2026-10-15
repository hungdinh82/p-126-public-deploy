"""Measure the real semantic prompt and verify pending speech on a Jetson API.

Run from the laptop checkout; no workflow state is stored on the AI server.
Only synthetic inputs and timing statistics are written to the report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path

import httpx

from src.vivi.capabilities.registry import catalog
from src.vivi.config import get_settings
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SessionState, TaskStep
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver, warmup_local_client
from src.vivi.rag.generator import LocalHandbookGenerator


async def benchmark(args):
    config = get_settings().model_copy(update={'local_llm_base_url': args.url.rstrip('/') + '/v1'})
    client = LocalHandbookGenerator(config).client
    client.max_tokens = min(config.planner_max_tokens, config.local_llm_max_tokens)
    client.max_attempts = 1
    capabilities = catalog(False, conversation=True, information=True)
    await warmup_local_client(client, capabilities)
    original = client._acomplete
    usage = {}

    async def recorded(payload):
        response = await original(payload)
        body = response.json()
        usage.update(body.get('usage', {}))
        return response

    client._acomplete = recorded
    resolver = SemanticResolver({'local': ModelProvider('local', client, 180)}, requested_budgets={'local': 180})
    old = 'Một cục nính xe ở đâu rồi?'
    completed = SessionState(active=DialogueTask(steps=[TaskStep(status='completed', draft=ActionDraft(
        capability='conversation.respond', arguments={'query': old}))]),
        conversation_context={'capability':'conversation.respond', 'query':old})
    cases = [
        ('temperature', 'đặt nhiệt độ 24 độ', SessionState()),
        ('window', 'mở cửa kính bên tài', SessionState()),
        ('merged_correction', 'đặt nhiệt độ 24 độ\nà không, đặt 26 độ\nmở cửa kính bên phụ', SessionState()),
        ('unclear_repetition', 'Mở gửi xe xa cho tôi\nMở gửi xe xa cho tôi', SessionState()),
        ('cold_after_old_topic', 'Tôi lạnh quá!', completed),
    ]
    if args.cases:
        cases = [case for case in cases if case[0] in args.cases]
    report = {'url':args.url, 'rounds_per_case':args.rounds, 'output_limit':client.max_tokens, 'cases':{}}
    async with httpx.AsyncClient(timeout=10, trust_env=False) as monitoring:
        initial = (await monitoring.get(args.url.rstrip('/') + '/v1/capabilities')).json()
        report['initial_memory'] = initial.get('system_memory')
        for name, text, session in cases:
            rows = []
            for index in range(args.rounds + 1):
                usage.clear()
                start = time.perf_counter()
                frame, served, attempts = await resolver.resolve(text, session, requested='local',
                    history=[{'query':old, 'answer':'Câu trả lời cũ'}] if session is completed else [],
                    catalog=capabilities)
                assert served == 'local', (name, attempts)
                actions = [(a.capability, a.arguments.model_dump(exclude_none=True)) for a in frame.actions]
                if name == 'temperature':
                    assert actions == [('climate.set_temperature', {'value_celsius':24.0})], actions
                elif name == 'window':
                    assert actions == [('window.set_position', {'zone':'driver', 'position_percent':100})], actions
                elif name == 'merged_correction':
                    assert actions == [('climate.set_temperature', {'value_celsius':26.0}),
                                       ('window.set_position', {'zone':'front_passenger', 'position_percent':100})], actions
                elif name == 'unclear_repetition':
                    assert frame.act == 'clarify' and not actions, actions
                else:
                    assert actions == [('climate.set_temperature', {'temperature_delta_celsius':2.0})], actions
                elapsed = round((time.perf_counter()-start)*1000, 2)
                if index:  # A case-specific warmup is excluded from statistics.
                    rows.append({'wall_ms':elapsed, 'prompt_tokens':usage.get('prompt_tokens'),
                                 'completion_tokens':usage.get('completion_tokens'),
                                 'guarded':any(a.get('semantic_guard') for a in attempts)})
                print(json.dumps({'case':name, 'run':index, 'wall_ms':elapsed, 'correct':True}), flush=True)
            times = sorted(row['wall_ms'] for row in rows)
            report['cases'][name] = {'median_ms':round(statistics.median(times),2),
                'p95_ms':times[math.ceil(.95*len(times))-1], 'runs':rows}
            caps = (await monitoring.get(args.url.rstrip('/') + '/v1/capabilities')).json()
            report['cases'][name]['memory_after'] = caps.get('system_memory')
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'summary':{name:{k:v for k,v in row.items() if k!='runs'}
                               for name,row in report['cases'].items()}}, ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://100.123.27.125:8795')
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--cases', nargs='+', choices=['temperature', 'window', 'merged_correction',
                                                      'unclear_repetition', 'cold_after_old_topic'])
    parser.add_argument('--output', type=Path, default=Path('/tmp/jetson-planner-benchmark.json'))
    arguments = parser.parse_args()
    if arguments.rounds < 1:
        parser.error('--rounds must be positive')
    asyncio.run(benchmark(arguments))
