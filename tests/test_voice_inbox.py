"""Accepted voice survives slow inference, retries, disconnects and restart."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.vivi.speech.inbox import InboxFullError, VoiceInbox
from src.vivi.speech.input_service import VoiceInputService


def test_receipt_is_idempotent_and_survives_restart(tmp_path):
    inbox=VoiceInbox(tmp_path)
    job=inbox.accept('driver','one',{},audio=b'recorded audio',suffix='.wav')
    assert Path(job['audio_path']).read_bytes()==b'recorded audio'
    assert inbox.accept('driver','one',{},audio=b'recorded audio')['id']==job['id']
    with pytest.raises(ValueError):
        inbox.accept('driver','one',{},audio=b'different')
    restarted=VoiceInbox(tmp_path)
    assert restarted.get(job['id'],'driver')['status']=='received'
    assert restarted.get(job['id'],'another-driver') is None
    restarted.claim('stt')
    restarted.recover()
    assert restarted.get(job['id'],'driver')['status']=='received'


def test_history_committed_before_execution_and_ordered_per_session(tmp_path):
    inbox=VoiceInbox(tmp_path)
    first=inbox.accept('driver','one',{},audio=b'one')
    second=inbox.accept('driver','two',{},transcript='two')
    assert inbox.claim('turn') is None
    inbox.update(first['id'],status='recognized',transcript='one')
    claimed=inbox.claim('turn')
    assert claimed['id']==first['id'] and claimed['transcript']=='one'
    assert inbox.claim('turn') is None
    inbox.update(first['id'],status='completed',response={'message':'done'})
    assert inbox.claim('turn')['id']==second['id']
    assert len(inbox.list('driver'))==2
    inbox.clear('driver')
    assert not inbox.list('driver') and not Path(first['audio_path']).exists()


def test_capacity_refuses_unacknowledged_work_without_losing_accepted_audio(tmp_path):
    inbox=VoiceInbox(tmp_path,max_pending=1)
    first=inbox.accept('driver','one',{},audio=b'one')
    with pytest.raises(InboxFullError):
        inbox.accept('driver','two',{},audio=b'two')
    assert inbox.get(first['id'],'driver')['status']=='received'
    assert len(list(tmp_path.glob('*.webm')))==1


async def wait_for(predicate,timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_rapid_speech_is_batched_into_one_ordered_snapshot(tmp_path):
    entered,release=asyncio.Event(),asyncio.Event()
    calls=[]
    events=[]
    class STT:
        async def transcribe(self,path):
            return path.read_bytes().decode()
    class Orchestrator:
        ledger=None
        default_provider='local'
        graph_providers={'local'}
        semantic_providers={'local'}
        active_task=None
        async def run(self,request,provider):
            self.active_task=asyncio.current_task()
            calls.append(request.transcript)
            if len(calls)==1:
                entered.set()
            try:
                await release.wait()
                return SimpleNamespace(model_dump=lambda **_: {'turn_id':request.turn_id,'message':'done'})
            finally:
                self.active_task=None
        def cancel(self,session_id,turn_id):
            self.active_task.cancel()
            return {'cancelled':True,'dispatched':False}
    config=SimpleNamespace(data_dir=tmp_path,store_audio=False,jetson_ai_policy='disabled',jetson_ai_url='')
    runtime=SimpleNamespace(settings=config,stt=STT(),orchestrator=Orchestrator(),
                            store=SimpleNamespace(append_event=events.append))
    service=VoiceInputService(runtime)
    jobs=[service.inbox.accept('driver',str(i),{},audio=f'utterance {i}'.encode()) for i in range(3)]
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(),2)
        await wait_for(lambda:len(events)==3)
        assert [service.inbox.get(j['id'],'driver')['transcript'] for j in jobs]==[f'utterance {i}' for i in range(3)]
        assert all(not Path(j['audio_path']).exists() for j in jobs)
        release.set()
        await wait_for(lambda:len(calls)==1)
        await wait_for(lambda:all(service.inbox.get(j['id'],'driver')['status'] in
                                  {'completed','superseded','failed'} for j in jobs))
        assert all(service.inbox.get(j['id'],'driver')['status'] in {'completed','superseded'} for j in jobs)
        processed='\n'.join(calls)
        assert all(f'utterance {i}' in processed for i in range(3))
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('registered', [True, False])
async def test_two_new_utterances_replace_running_qwen_with_one_combined_input(tmp_path, registered):
    entered, never_release = asyncio.Event(), asyncio.Event()
    calls=[]
    active=maximum_active=0
    class STT:
        async def transcribe(self,path):
            return path.read_bytes().decode()
    class Orchestrator:
        ledger=None
        default_provider='local'
        graph_providers={'local'}
        semantic_providers={'local'}
        active_task=None
        async def run(self,request,provider):
            nonlocal active,maximum_active
            self.active_task=asyncio.current_task()
            active+=1
            maximum_active=max(maximum_active,active)
            calls.append(request.transcript)
            try:
                if len(calls)==1:
                    entered.set()
                    await never_release.wait()
                return SimpleNamespace(model_dump=lambda **_: {'turn_id':request.turn_id,'message':'current answer'})
            finally:
                active-=1
                self.active_task=None
        def cancel(self,session_id,turn_id):
            if registered:
                self.active_task.cancel()
            return {'cancelled':registered,'dispatched':False}
    config=SimpleNamespace(data_dir=tmp_path,store_audio=False,jetson_ai_policy='disabled',jetson_ai_url='')
    orchestrator=Orchestrator()
    runtime=SimpleNamespace(settings=config,stt=STT(),orchestrator=orchestrator,
                            store=SimpleNamespace(append_event=lambda _:None))
    service=VoiceInputService(runtime)
    first=service.inbox.accept('driver','first',{},audio=b'first utterance')
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(),2)
        second=service.inbox.accept('driver','second',{},audio=b'second correction')
        third=service.inbox.accept('driver','third',{},audio=b'third addition')
        await wait_for(lambda:service.inbox.get(third['id'],'driver')['status']=='completed',3)
        assert calls==['first utterance','first utterance\nsecond correction\nthird addition']
        assert maximum_active==1
        assert service.inbox.get(first['id'],'driver')['status']=='completed'
        assert service.inbox.get(second['id'],'driver')['status']=='completed'
        assert service.inbox.get(third['id'],'driver')['status']=='completed'
        assert len({service.inbox.get(job['id'],'driver')['generation_turn_id']
                    for job in (first,second,third)}) == 1
    finally:
        never_release.set()
        await service.close()


@pytest.mark.asyncio
async def test_predispatch_failure_keeps_transcript_pending_and_retries(tmp_path):
    attempts = []

    class Ledger:
        def turn_status(self, *args):
            return {'status': 'interrupted', 'command': None}

    class Orchestrator:
        ledger = Ledger()
        profile_id = 'test'
        default_provider = 'rules'
        graph_providers = semantic_providers = {'rules'}

        async def run(self, request, provider):
            attempts.append(request.transcript)
            if len(attempts) == 1:
                raise ConnectionError('backend unavailable before dispatch')
            return SimpleNamespace(model_dump=lambda **_: {'turn_id':request.turn_id,'message':'recovered'})

    settings = SimpleNamespace(data_dir=tmp_path, store_audio=False, jetson_ai_policy='disabled', jetson_ai_url='')
    service = VoiceInputService(SimpleNamespace(settings=settings, stt=None, orchestrator=Orchestrator(),
                                               store=SimpleNamespace(append_event=lambda _: None)))
    job = service.inbox.accept('driver', 'first', {}, transcript='đặt nhiệt độ 26 độ')
    await service.start()
    try:
        await wait_for(lambda:service.inbox.get(job['id'],'driver')['attempts'] == 1)
        saved = service.inbox.get(job['id'],'driver')
        assert saved['status'] == 'recognized' and saved['transcript'] == 'đặt nhiệt độ 26 độ'
        await wait_for(lambda:service.inbox.get(job['id'],'driver')['status'] == 'completed', 5)
        assert attempts == ['đặt nhiệt độ 26 độ'] * 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_shutdown_waits_for_inference_cleanup_and_keeps_pending_text(tmp_path):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def run(request, provider):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    settings = SimpleNamespace(data_dir=tmp_path, store_audio=False, jetson_ai_policy='disabled', jetson_ai_url='')
    orchestrator = SimpleNamespace(ledger=None, default_provider='local', graph_providers={'local'},
        semantic_providers={'local'}, run=run, cancel=lambda *_:{'cancelled':False,'dispatched':False})
    service = VoiceInputService(SimpleNamespace(settings=settings, stt=None, orchestrator=orchestrator,
        store=SimpleNamespace(append_event=lambda _:None)))
    job = service.inbox.accept('driver', 'first', {}, transcript='tôi vẫn đang nói')
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
    finally:
        await service.close()
    assert stopped.is_set()
    assert service.inbox.get(job['id'],'driver')['status'] == 'recognized'
    assert service.inbox.get(job['id'],'driver')['transcript'] == 'tôi vẫn đang nói'
