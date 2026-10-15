"""On-device TTS-only benchmark using the production adapter and selected profile.

Run each precision in a fresh process with the Jetson SDK environment activated.
This avoids keeping multiple model copies resident on Xavier's shared RAM.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

from jetson_ai.speech import SpeechModels, available_memory_mb


def process_memory():
    result = {}
    for line in Path('/proc/self/status').read_text().splitlines():
        key = line.split(':', 1)[0]
        if key in {'VmRSS', 'VmHWM'}:
            result[key + '_kb'] = int(line.split()[1])
    return result


def run(args):
    import numpy as np
    report = {'text':args.text, 'warm_runs':args.rounds, 'seed':42,
              'scope':'TTS only; SLM and STT not resident', 'memory_before_mb':available_memory_mb()}
    speech = SpeechModels()
    speech.load('tts')
    state = speech.info['tts']
    report['load'] = dict(state)
    report['memory_after_load_mb'] = available_memory_mb()
    if state['status'] != 'ready':
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        raise RuntimeError(state['error'])
    rows = []
    for index in range(args.rounds + 1):
        np.random.seed(42)
        started = time.perf_counter()
        stream = speech.synthesize_stream(args.text)
        first_ms, size, count, nonzero = None, 0, 0, False
        try:
            for chunk in stream:
                if first_ms is None:
                    first_ms = (time.perf_counter()-started)*1000
                size += len(chunk)
                count += 1
                nonzero |= any(chunk)
        finally:
            stream.close()
        elapsed = (time.perf_counter()-started)*1000
        if not size or not nonzero:
            raise RuntimeError('Empty or silent TTS output')
        duration_ms = size / (2 * state['sample_rate']) * 1000
        row = {'first_audio_ms':round(first_ms,2), 'total_ms':round(elapsed,2),
               'audio_duration_ms':round(duration_ms,2), 'real_time_factor':round(elapsed/duration_ms,4),
               'chunks':count, 'audio_bytes':size, 'memory_available_mb':available_memory_mb(), **process_memory()}
        if index:
            rows.append(row)
        else:
            report['cold_request'] = row
        report['runs'] = rows
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'run':index, **row}),flush=True)
    report['summary'] = {}
    for name in ('first_audio_ms', 'total_ms', 'audio_duration_ms', 'real_time_factor'):
        values = sorted(row[name] for row in rows)
        report['summary'][name] = {'median':round(statistics.median(values),4),
                                  'p95':values[math.ceil(.95*len(values))-1]}
    report['summary'].update(process_memory(), min_available_mb=min(row['memory_available_mb'] for row in rows))
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'summary':report['summary']},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds',type=int,default=30)
    parser.add_argument('--text',default='Mình sẽ giúp bạn điều chỉnh nhiệt độ nhé.')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    run(args)
