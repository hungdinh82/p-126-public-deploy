"""Admission, model isolation, FIFO and decoder-step resource ownership."""
import asyncio
import io
import wave
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Request

from jetson_ai.app import ServiceConfig, create_app
from jetson_ai.scheduler import InferenceScheduler, SchedulingError
from jetson_ai.stream_routes import ModelStreams


def wav_bytes():
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setparams((1,2,16000,0,'NONE','not compressed'))
        wav.writeframes(b'\0\0'*1600)
    return output.getvalue()


@pytest.mark.asyncio
async def test_long_llm_does_not_reject_stt_and_busy_model_still_accepts_queue():
    entered,release=asyncio.Event(),asyncio.Event()
    async def backend(request):
        if request.url.path=='/health':
            return httpx.Response(200,json={'status':'ok'})
        entered.set()
        await release.wait()
        return httpx.Response(200,json={'choices':[{'message':{'content':'done'}}]})
    app=create_app(ServiceConfig(token='',auth_required=False),transport=httpx.MockTransport(backend))
    app.state.speech.info['stt']['status']='ready'
    app.state.speech.transcribe=lambda _: 'Lời nói vẫn được nhận dạng'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        llm=asyncio.create_task(client.post('/v1/chat/completions',json={'messages':[{'role':'user','content':'slow'}]}))
        try:
            await asyncio.wait_for(entered.wait(),1)
            cap=(await client.get('/v1/capabilities')).json()['capabilities']
            assert cap['llm']['status']=='busy' and cap['llm']['accepting_requests']
            responses=await asyncio.gather(*(client.post('/v1/stt/transcriptions',files={'file':('input.wav',wav_bytes(),'audio/wav')}) for _ in range(5)))
            assert [r.status_code for r in responses] == [200] * len(responses), [r.text for r in responses]
            assert not llm.done()
        finally:
            release.set()
            await llm
        assert not app.state.scheduler._active and not app.state.scheduler._pending


@pytest.mark.asyncio
async def test_queue_is_fifo_bounded_and_cancelled_waiter_does_not_leak():
    scheduler=InferenceScheduler(queue_limits={'llm':2,'stt':2,'tts':2})
    first=await scheduler.acquire('llm')
    second=asyncio.create_task(scheduler.acquire('llm'))
    third=asyncio.create_task(scheduler.acquire('llm'))
    await asyncio.sleep(0)
    with pytest.raises(SchedulingError,match='full'):
        await scheduler.acquire('llm')
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    first.release()
    lease=await third
    lease.release()
    assert scheduler.snapshot('llm')['queue_depth']==0
    assert scheduler.snapshot('llm')['active_requests']==0


@pytest.mark.asyncio
async def test_queue_timeout_releases_only_its_own_reservation():
    scheduler=InferenceScheduler(queue_timeouts={'llm':.01,'stt':.01,'tts':.01})
    first=await scheduler.acquire('llm')
    with pytest.raises(SchedulingError) as exc:
        await scheduler.acquire('llm')
    assert exc.value.code=='QUEUE_TIMEOUT'
    assert scheduler.snapshot('llm')['active_requests']==1
    first.release()
    async with await scheduler.acquire('llm'):
        pass


@pytest.mark.asyncio
async def test_independent_policy_admits_three_models_but_serializes_each_engine():
    scheduler=InferenceScheduler(policy='independent')
    leases=await asyncio.gather(*(scheduler.acquire(name) for name in ('stt','llm','tts')))
    pending=asyncio.create_task(scheduler.acquire('stt'))
    await asyncio.sleep(0)
    assert not pending.done()
    leases[0].release()
    (await pending).release()
    for lease in leases:
        lease.release()
    assert not scheduler._active


@pytest.mark.asyncio
async def test_tts_yields_speech_lane_between_chunks_without_second_tts_generator():
    class Speech:
        info={'tts':{'status':'ready','sample_rate':16000}}
        def synthesize_stream(self,text):
            try:
                yield b'\1\0'
                yield b'\2\0'
            finally:
                self.closed=True
    speech=Speech()
    scheduler=InferenceScheduler()
    streams=ModelStreams(SimpleNamespace(timeout_seconds=2),speech,scheduler,None,None,None)
    response=await streams.tts('hello',Request({'type':'http','headers':[]}))
    assert await anext(response.body_iterator)==b'\1\0'
    stt=await asyncio.wait_for(scheduler.acquire('stt'),1)
    another_tts=asyncio.create_task(scheduler.acquire('tts'))
    next_chunk=asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(.01)
    assert not next_chunk.done() and not another_tts.done()
    stt.release()
    assert await next_chunk==b'\2\0'
    await response.body_iterator.aclose()
    (await another_tts).release()
    assert speech.closed and not scheduler._active and not scheduler._pending
