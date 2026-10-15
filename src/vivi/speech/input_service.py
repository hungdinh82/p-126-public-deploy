"""Independent STT and ordered dialogue consumers for durable voice inputs."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import Field

from src.vivi.ai_router import JetsonProviderRouter
from src.vivi.domain.models import TurnRequest
from src.vivi.speech.inbox import VoiceInbox

logger = logging.getLogger(__name__)
MAX_SNAPSHOT_CHARS = 10000


class VoiceTurnRequest(TurnRequest):
    """Long local snapshots do not expand main's public turn API contract."""

    transcript: str = Field(min_length=1, max_length=MAX_SNAPSHOT_CHARS)


class VoiceInputService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.inbox = VoiceInbox(runtime.settings.data_dir / 'runtime' / 'voice-inputs')
        self.router = JetsonProviderRouter(runtime.settings)
        self.tasks = []
        self.stopping = False
        self._revisions: dict[str, asyncio.Event] = {}

    async def start(self):
        self.stopping = False
        interrupted = self.inbox.recover()
        for job in interrupted:
            ledger = self.runtime.orchestrator.ledger
            generation_id=job.get('generation_turn_id') or job['turn_id']
            value = await asyncio.to_thread(ledger.turn_status,
                f'{self.runtime.orchestrator.profile_id}:{job["session_id"]}', generation_id, job['session_id']) if ledger else None
            if value and value['response'] and not value['response'].get('history_cleared'):
                await asyncio.to_thread(self.inbox.settle_conversation,[job['id']],status='completed',
                                         response=value['response'],response_revision=job['revision'])
            elif value is None or (value['status']=='interrupted' and value['command'] is None):
                await asyncio.to_thread(self.inbox.requeue_conversation,[job['id']])
            else:
                await asyncio.to_thread(self.inbox.update,job['id'],status='interrupted',
                    error='Lượt bị gián đoạn; transcript đã lưu. Kiểm tra trạng thái thao tác trước khi gửi lượt mới.')
        self.tasks = [asyncio.create_task(self._consume('stt')), asyncio.create_task(self._consume('turn'))]

    async def close(self):
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.tasks = []

    async def _consume(self, stage):
        while not self.stopping:
            try:
                job = self.inbox.claim(stage)
                if job is None:
                    await asyncio.sleep(.25)
                    continue
                if stage == 'stt':
                    await self._transcribe(job)
                else:
                    await self._turn(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Voice %s worker failed; pending input remains durable',stage)
                await asyncio.sleep(1)

    async def _transcribe(self, job):
        try:
            text = await self.runtime.stt.transcribe(Path(job['audio_path']))
            if not isinstance(text,str) or not text.strip():
                raise ValueError('Không nhận diện được lời nói')
            text = text.strip()
        except Exception as exc:
            attempts = job['attempts'] + 1
            retryable = isinstance(exc,(httpx.HTTPError,RuntimeError))
            await asyncio.to_thread(self.inbox.update,job['id'], status='received' if retryable else 'failed',
                error=str(exc),attempts=attempts,retry_at=time.time()+min(30,2**min(attempts,5)))
            return
        # Persist text before emitting history or admitting work to the LLM.
        job['revision'] = self.inbox.complete_transcription(job['id'],text)
        self.runtime.store.append_event({'event_type':'stt','type':'stt','session_id':job['session_id'],
            'turn_id':job['turn_id'],'transcript':text,'voice_input_id':job['id'],'revision':job['revision']})
        self._revision_event(job['session_id']).set()
        if not self.runtime.settings.store_audio:
            await asyncio.to_thread(self.inbox.discard_audio,job)

    def _revision_event(self, session_id):
        return self._revisions.setdefault(session_id,asyncio.Event())

    def transcript_received(self, session_id):
        """Wake an in-flight generation when a final transcript is committed."""
        self._revision_event(session_id).set()

    async def _turn(self,job):
        while await self._turn_snapshot(job):
            pass

    async def _turn_snapshot(self,job):
        session_id=job['session_id']
        changed=self._revision_event(session_id)
        requested = job['context'].get('llm_provider') or self.runtime.orchestrator.default_provider
        available = getattr(self.runtime.orchestrator,'semantic_providers',self.runtime.orchestrator.graph_providers)
        provider = await self.router.select(requested, available)
        # Coalesce short consecutive utterances, but start within a bounded
        # window even if the driver keeps speaking.
        first=time.monotonic()
        while provider == 'local' and time.monotonic()-first < 1.5:
            changed.clear()
            try:
                await asyncio.wait_for(changed.wait(),timeout=min(.45,1.5-(time.monotonic()-first)))
            except TimeoutError:
                break
        rows=self.inbox.recognized_for_session(session_id)
        if job['id'] not in {row['id'] for row in rows}:
            rows=[job,*rows]
        rows.sort(key=lambda value:value['sequence'])
        bounded=[]
        chars=0
        for row in rows:
            size=len(row['transcript'])+(1 if bounded else 0)
            if bounded and chars+size>MAX_SNAPSHOT_CHARS:
                break
            bounded.append(row)
            chars+=size
        rows=bounded
        identities=[row['id'] for row in rows]
        revision=max(row['revision'] for row in rows)
        changed.clear()
        newly_ready=self.inbox.recognized_for_session(session_id)
        if any(row['revision']>revision for row in newly_ready):
            self._revision_event(session_id).set()
            return True
        generation_id='voice-'+uuid4().hex
        rows=self.inbox.claim_conversation(session_id,identities,generation_id)
        if not rows:
            return
        transcript='\n'.join(row['transcript'] for row in rows)
        context={**rows[0]['context'],**rows[-1]['context']}
        context['llm_provider']=rows[-1]['context'].get('llm_provider') or rows[0]['context'].get('llm_provider')
        request = VoiceTurnRequest(session_id=session_id,turn_id=generation_id,transcript=transcript,**context)
        generation = None
        try:
            generation=asyncio.create_task(self.runtime.orchestrator.run(request,provider=provider))
            revision_changed=asyncio.create_task(changed.wait())
            try:
                done,_=await asyncio.wait({generation,revision_changed},return_when=asyncio.FIRST_COMPLETED)
                if revision_changed in done and generation not in done:
                    cancellation=self.runtime.orchestrator.cancel(session_id,generation_id)
                    if not cancellation.get('dispatched'):
                        # The turn may still be awaiting its session lock or
                        # ledger admission, before cancel() can find it. Stop
                        # that queued coroutine as well as active inference.
                        if not cancellation.get('cancelled'):
                            generation.cancel()
                        try:
                            await generation
                        except asyncio.CancelledError:
                            pass
                        self.inbox.requeue_conversation(identities)
                        return True
                response=await generation
            finally:
                revision_changed.cancel()
                await asyncio.gather(revision_changed,return_exceptions=True)
            value=response.model_dump(mode='json')
            latest=self.inbox.recognized_for_session(session_id)
            stale=any(row['revision']>revision for row in latest)
            self.inbox.settle_conversation(identities,
                                     status='superseded' if stale else 'completed',response=value,
                                     response_revision=revision)
            if stale:
                self._revision_event(session_id).set()
        except asyncio.CancelledError:
            if generation is not None and not generation.done():
                cancellation = self.runtime.orchestrator.cancel(session_id, generation_id)
                if cancellation.get('dispatched'):
                    # Leave the processing binding for ledger reconciliation;
                    # replaying a dispatched effect after restart is unsafe.
                    raise
                if not cancellation.get('cancelled'):
                    generation.cancel()
                await asyncio.gather(generation, return_exceptions=True)
            self.inbox.requeue_conversation(identities)
            raise
        except Exception as exc:
            ledger = self.runtime.orchestrator.ledger
            entry = await asyncio.to_thread(ledger.turn_status,
                f'{self.runtime.orchestrator.profile_id}:{session_id}', generation_id, session_id) if ledger else None
            # Retry only when the ledger proves no effect was dispatched.
            # Keep every transcript pending; a fresh snapshot can include speech
            # that arrived during an unavailable backend or failed generation.
            if ledger and (entry is None or entry['command'] is None):
                attempts = max(row['attempts'] for row in rows) + 1
                self.inbox.requeue_conversation(identities, error=str(exc), retry=True,
                    retry_at=time.time()+min(30,2**min(attempts,5)))
                return
            self.inbox.settle_conversation(identities,status='failed',error=str(exc))
