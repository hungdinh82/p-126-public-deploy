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
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.local_tools import proposals_to_turn
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.rag.generator import LocalHandbookGenerator


def correct_frame(case, frame):
    """Score model output independently of guards, validation repair and fallback."""
    if frame is None:
        return False
    try:
        frame = SemanticTurn.model_validate(frame) if isinstance(frame, dict) else frame
    except ValueError:
        return False
    actions = [(a.capability, a.arguments.model_dump(exclude_none=True)) for a in frame.actions]
    if case == 'unclear_repetition':
        return frame.act == 'clarify' and not actions
    if case == 'negated_trunk':
        return not frame.drafts()
    expected = {
        'music_play_generic': [('media.play', {})],
        'music_play_query': [('media.play', {'media_query': 'thư giãn'})],
        'music_pause': [('media.pause', {})],
        'open_trunk': [('trunk.set_open', {'open': True})],
        'temperature': [('climate.set_temperature', {'value_celsius': 24.0})],
        'window': [('window.set_position', {'zone': 'driver', 'position_percent': 100})],
        'merged_correction': [('climate.set_temperature', {'value_celsius': 26.0}),
                              ('window.set_position', {'zone': 'front_passenger', 'position_percent': 100})],
        'cold_after_old_topic': [('climate.set_temperature', {'temperature_delta_celsius': 2.0})],
        'cold_after_trunk': [('climate.set_temperature', {'temperature_delta_celsius': 2.0})],
        'relative_increase': [('climate.set_temperature', {'temperature_delta_celsius': 2.0})],
        'trunk_and_window': [('trunk.set_open', {'open': True}),
                             ('window.set_position', {'zone': 'driver', 'position_percent': 100})],
    }
    return frame.act == 'request' and actions == expected[case]


async def benchmark(args):
    config = get_settings().model_copy(update={'local_llm_base_url': args.url.rstrip('/') + '/v1'})
    client = LocalHandbookGenerator(config).client
    client.max_tokens = min(config.planner_max_tokens, config.local_llm_max_tokens)
    client.max_attempts = 1
    capabilities = catalog(getattr(args, 'navigation_catalog', False), conversation=True, information=True)
    original = client._acomplete
    usage = {}
    raw_outputs = []
    raw_tool_calls = []
    response_usage = []

    async def recorded(payload):
        response = await original(payload)
        body = response.json()
        usage.update(body.get('usage', {}))
        response_usage.append(body.get('usage', {}))
        try:
            calls = client._tool_results(response)
            raw_tool_calls.append(calls)
            if len(calls) == 1 and calls[0][0] == 'interpret_turn':
                raw = calls[0][1]
            else:
                include_suggestions = any(item['function']['name'] == 'dialogue_suggest_unrequested_action'
                                          for item in payload.get('tools', []))
                raw = proposals_to_turn(calls, capabilities, include_suggestions=include_suggestions, utterance=text).model_dump(mode='json', exclude_defaults=True)
        except (ValueError, KeyError, TypeError):
            raw = None
        raw_outputs.append(raw)
        return response

    client._acomplete = recorded
    resolver = SemanticResolver({'local': ModelProvider('local', client, 180)}, requested_budgets={'local': 180})
    old = 'Một cục nính xe ở đâu rồi?'
    completed = SessionState(active=DialogueTask(steps=[TaskStep(status='completed', draft=ActionDraft(
        capability='conversation.respond', arguments={'query': old}))]),
        conversation_context={'capability':'conversation.respond', 'query':old})
    completed_trunk = SessionState(active=DialogueTask(steps=[TaskStep(status='completed', draft=ActionDraft(
        capability='trunk.set_open', arguments={'open': True}))]))
    cases = [
        ('music_play_generic', 'bật nhạc', SessionState()),
        ('music_play_query', 'phát nhạc thư giãn', SessionState()),
        ('music_pause', 'tạm dừng nhạc', SessionState()),
        ('open_trunk', 'mở cốp xe', SessionState()),
        ('temperature', 'đặt nhiệt độ 24 độ', SessionState()),
        ('window', 'mở cửa kính bên tài', SessionState()),
        ('merged_correction', 'đặt nhiệt độ 24 độ\nà không, đặt 26 độ\nmở cửa kính bên phụ', SessionState()),
        ('unclear_repetition', 'Mở gửi xe xa cho tôi\nMở gửi xe xa cho tôi', SessionState()),
        ('cold_after_old_topic', 'Tôi lạnh quá!', completed),
        ('cold_after_trunk', 'Tôi lạnh quá!', completed_trunk),
        ('relative_increase', 'tăng nhiệt độ điều hòa lên', SessionState()),
        ('trunk_and_window', 'mở cốp xe\nmở cửa sổ bên tài', SessionState()),
        ('negated_trunk', 'đừng mở cốp xe', SessionState()),
    ]
    if args.cases:
        cases = [case for case in cases if case[0] in args.cases]
    report = {'url':args.url, 'rounds_per_case':args.rounds, 'output_limit':client.max_tokens,
              'navigation_catalog':getattr(args, 'navigation_catalog', False),
              'local_contract':'local-proposals-v2',
              'token_usage_scope':'prompt_tokens/completion_tokens describe last response; response_usage and total_completion_tokens include all calls', 'cases':{}}
    async with httpx.AsyncClient(timeout=10, trust_env=False) as monitoring:
        initial = (await monitoring.get(args.url.rstrip('/') + '/v1/capabilities')).json()
        report['initial_memory'] = initial.get('system_memory')
        for name, text, session in cases:
            rows = []
            cold = None
            for index in range(args.rounds + 1):
                usage.clear()
                raw_outputs.clear()
                raw_tool_calls.clear()
                response_usage.clear()
                start = time.perf_counter()
                frame, served, attempts = await resolver.resolve(text, session, requested='local',
                    history=([{'query':old, 'answer':'Câu trả lời cũ'}] if session is completed else
                             [{'query':'mở cốp xe', 'answer':'Đã mở cốp xe.'}] if session is completed_trunk else []),
                    catalog=capabilities)
                final_correct = correct_frame(name, frame)
                raw = raw_outputs[-1] if raw_outputs else None
                raw_correct = correct_frame(name, raw) if raw_outputs else None
                elapsed = round((time.perf_counter()-start)*1000, 2)
                row = {'wall_ms':elapsed, 'prompt_tokens':usage.get('prompt_tokens'),
                       'completion_tokens':usage.get('completion_tokens'),
                       'guarded':any(a.get('semantic_guard') for a in attempts),
                       'backend_calls':len(response_usage), 'response_usage':list(response_usage),
                       'total_completion_tokens':sum(item.get('completion_tokens', 0) for item in response_usage),
                       'served':served, 'model_called':bool(raw_outputs),
                       'raw_correct':raw_correct, 'final_correct':final_correct,
                       'raw_outputs':list(raw_outputs), 'raw_tool_calls':list(raw_tool_calls), 'attempts':attempts}
                if index:
                    rows.append(row)
                else:
                    cold = row
                report['cases'][name] = {'cold_run':cold, 'runs':rows}
                args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps({'case':name, 'run':index, 'wall_ms':elapsed, 'raw_correct':raw_correct, 'final_correct':final_correct, 'served':served}), flush=True)
            times = sorted(row['wall_ms'] for row in rows)
            report['cases'][name] = {'median_ms':round(statistics.median(times),2),
                'p95_ms':times[math.ceil(.95*len(times))-1],
                'raw_evaluated_runs':sum(row['model_called'] for row in rows),
                'raw_correct_runs':sum(row['raw_correct'] is True for row in rows),
                'local_served_runs':sum(row['served'] == 'local' for row in rows),
                'final_correct_runs':sum(row['final_correct'] for row in rows), 'cold_run':cold, 'runs':rows}
            caps = (await monitoring.get(args.url.rstrip('/') + '/v1/capabilities')).json()
            report['cases'][name]['memory_after'] = caps.get('system_memory')
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'summary':{name:{k:v for k,v in row.items() if k not in {'runs', 'cold_run'}}
                               for name,row in report['cases'].items()}}, ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://100.123.27.125:8795')
    parser.add_argument('--rounds', type=int, default=30)
    parser.add_argument('--cases', nargs='+', choices=['music_play_generic', 'music_play_query', 'music_pause', 'open_trunk', 'temperature', 'window', 'merged_correction',
                                                      'unclear_repetition', 'cold_after_old_topic', 'cold_after_trunk',
                                                      'relative_increase', 'trunk_and_window', 'negated_trunk'])
    parser.add_argument('--navigation-catalog', action='store_true', help='Include navigation tool documentation; no tools execute.')
    parser.add_argument('--output', type=Path, default=Path('/tmp/jetson-planner-benchmark.json'))
    arguments = parser.parse_args()
    if arguments.rounds < 1:
        parser.error('--rounds must be positive')
    asyncio.run(benchmark(arguments))
