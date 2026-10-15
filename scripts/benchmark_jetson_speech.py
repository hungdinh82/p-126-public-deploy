"""On-device speech latency, contention and real upstream cancellation benchmark."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import math
import statistics
import subprocess
import time
import wave
from pathlib import Path

import httpx


def summary(rows):
    values = sorted(row['wall_ms'] for row in rows)
    return {'median_ms':round(statistics.median(values),2),
            'p95_ms':values[math.ceil(.95*len(values))-1], 'runs':rows}


async def benchmark(args):
    report = {'rounds':args.rounds, 'cases':{}}
    async with httpx.AsyncClient(base_url=args.url, trust_env=False, timeout=180) as client:
        caps = (await client.get('/v1/capabilities')).json()
        report['stt_device'] = caps['capabilities']['stt']['device']
        report['tts_precision'] = caps['capabilities']['tts'].get('precision')
        report['initial_memory'] = caps.get('system_memory')
        if args.audio_file.exists():
            wav = args.audio_file.read_bytes()
        else:
            response = await client.post('/v1/tts/synthesize',json={'text':'Mở cửa kính bên tài.'})
            response.raise_for_status()
            raw = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i','pipe:0',
                                  '-ac','1','-ar','16000','-f','s16le','pipe:1'],
                                 input=response.content,capture_output=True,check=True).stdout
            buffer = io.BytesIO()
            with wave.open(buffer,'wb') as audio:
                audio.setparams((1,2,16000,0,'NONE','not compressed'))
                audio.writeframes(raw)
            wav = buffer.getvalue()
            args.audio_file.write_bytes(wav)
        report['audio_sha256'] = hashlib.sha256(wav).hexdigest()
        modes = ['stt_alone', 'stt_during_qwen'] + ([] if args.skip_tts else ['tts_stream'])
        for mode in modes:
            rows = []
            for index in range(args.rounds + 1):
                generation = None
                if mode == 'stt_during_qwen':
                    generation = asyncio.create_task(client.post('/v1/chat/completions',json={
                        'messages':[{'role':'user','content':'Hãy kể một câu chuyện tiếng Việt dài 1000 từ về thiên nhiên.'}],
                        'max_tokens':1024, 'temperature':0}))
                    for _ in range(300):
                        status = (await client.get('/v1/capabilities')).json()['capabilities']['llm']
                        if status['active_requests']:
                            break
                        await asyncio.sleep(.01)
                    else:
                        generation.cancel()
                        await asyncio.gather(generation,return_exceptions=True)
                        raise AssertionError('Qwen never became active')
                start = time.perf_counter()
                row = {}
                try:
                    if mode == 'tts_stream':
                        first_ms = None
                        async with client.stream('POST','/v1/tts/stream',json={
                            'text':'Mình sẽ giúp bạn điều chỉnh nhiệt độ nhé.'}) as response:
                            response.raise_for_status()
                            size = 0
                            async for chunk in response.aiter_bytes():
                                if chunk and first_ms is None:
                                    first_ms = (time.perf_counter()-start)*1000
                                size += len(chunk)
                        row = {'wall_ms':round((time.perf_counter()-start)*1000,2),
                               'first_audio_ms':round(first_ms,2), 'audio_bytes':size}
                    else:
                        response = await client.post('/v1/stt/transcriptions', files={'file':('input.wav',wav,'audio/wav')})
                        response.raise_for_status()
                        row = {'wall_ms':round((time.perf_counter()-start)*1000,2), 'transcript':response.json()['text'],
                               'queue_ms':response.json()['queue_ms']}
                        if generation:
                            row['qwen_still_running'] = not generation.done()
                finally:
                    if generation:
                        cancel_start = time.perf_counter()
                        generation.cancel()
                        await asyncio.gather(generation,return_exceptions=True)
                        for _ in range(300):
                            status = (await client.get('/v1/capabilities')).json()['capabilities']['llm']
                            if not status['active_requests']:
                                break
                            await asyncio.sleep(.01)
                        else:
                            raise AssertionError('Qwen did not acknowledge cancellation')
                        row['cancel_ms'] = round((time.perf_counter()-cancel_start)*1000,2)
                if index:
                    rows.append(row)
                print(json.dumps({'case':mode,'run':index, **row},ensure_ascii=False),flush=True)
            report['cases'][mode] = summary(rows)
            report['memory_after'] = (await client.get('/v1/capabilities')).json().get('system_memory')
            with open(args.output,'w') as output:
                json.dump(report,output,ensure_ascii=False,indent=2)
    print(json.dumps({'summary':{name:{k:v for k,v in rows.items() if k!='runs'}
                               for name,rows in report['cases'].items()}},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://100.123.27.125:8795')
    parser.add_argument('--rounds',type=int,default=30)
    parser.add_argument('--output',default='/tmp/jetson-speech-benchmark.json')
    parser.add_argument('--skip-tts',action='store_true')
    parser.add_argument('--audio-file',type=Path,default=Path('/tmp/vivi-voice-revision-stt.wav'))
    arguments = parser.parse_args()
    if arguments.rounds < 1:
        parser.error('--rounds must be positive')
    asyncio.run(benchmark(arguments))
