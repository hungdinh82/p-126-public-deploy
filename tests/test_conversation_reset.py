import asyncio
import json

import pytest

from eval.workflow_benchmark import Runtime
from src.vivi.domain.models import TurnRequest


@pytest.mark.parametrize('reply', ['Ok, bắt đầu.', 'Ok, xác nhận.'])
def test_navigation_confirmation_without_provider(tmp_path, reply):
    async def scenario():
        runtime = Runtime(tmp_path, 'fixture')
        try:
            await runtime.execute({'type': 'turn', 'id': 'plan', 'query': 'Chỉ đường tới Long Biên.', 'position': {'lat': 21, 'lng': 105.8, 'simulated': True}}, 'user', {})
            await runtime.execute({'type': 'turn', 'id': 'center', 'query': 'Không, đến trung tâm Long Biên.'}, 'user', {})
            preview = await runtime.execute({'type': 'turn', 'id': 'place', 'query': 'Đúng rồi'}, 'user', {})
            assert preview['navigation']['status'] == 'preview', preview['message']
            calls = runtime.model.calls
            response = await runtime.execute({'type': 'turn', 'id': 'approve', 'query': reply}, 'user', {})
            assert response['navigation']['status'] == 'running', response['message']
            assert runtime.model.calls == calls
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_reset_erases_context_preserves_effects_and_other_profiles(tmp_path):
    async def scenario():
        runtime = Runtime(tmp_path, 'rules')
        try:
            request = TurnRequest(session_id='user', turn_id='old', transcript='Nhiệt độ 24 độ')
            await runtime.pipeline.run(request)
            await runtime.pipeline.run(TurnRequest(session_id='older', turn_id='past', transcript='Xin chào'))
            history = runtime.graph.navigation.workflow.services.history
            history.save({'session_id': 'profile:other:session', 'turn_id': 'other', 'query': 'private'})
            store = runtime.pipeline.store
            store.config.store_transcripts = True
            store.append_event({'session_id': 'user', 'turn_id': 'old', 'transcript': 'secret'})
            store.append_event({'session_id': 'other-session', 'turn_id': 'other', 'transcript': 'keep'})
            import sqlite3

            backup = history.path.with_name(history.path.stem + '-before-trip-plan-test.sqlite3')
            with history._connect() as source, sqlite3.connect(backup) as target:
                source.backup(target)
            before = runtime.vehicle.state_for('user').model_dump()
            result = await runtime.pipeline.reset_history('user')
            assert result['deleted_turns'] == 2
            assert history.recent('older', 6) == []
            assert history.recent('user', 6) == []
            with sqlite3.connect(backup) as db:
                assert db.execute('SELECT session_id FROM handbook_turns').fetchall() == [('profile:other:session',)]
            assert len(history.recent('profile:other:session', 6)) == 1
            checkpoint = await runtime.graph.compiled.aget_state(runtime.graph.config({'session_id': 'user'}))
            assert not checkpoint.values
            assert runtime.vehicle.state_for('user').model_dump() == before
            records = [json.loads(line) for path in tmp_path.glob('????-??-??/turns.jsonl') for line in path.read_text().splitlines()]
            assert [record['transcript'] for record in records] == ['keep']
            store.append_event({'session_id': 'user', 'turn_id': 'old', 'transcript': 'late STT'})
            store.record_tts('user', 'old', {'synthesis_ms': 10})
            assert ('user', 'old') not in store.tts_by_turn
            assert all(event['session_id'] != 'user' for event in store.recent_events)
            with pytest.raises(ValueError, match='lịch sử đã xoá'):
                await runtime.pipeline.run(request)
            await runtime.pipeline.run(request.model_copy(update={'turn_id': 'new', 'transcript': 'Xin chào'}))
            assert len(history.recent('user', 6)) == 1
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_reset_rejects_active_turn(tmp_path):
    async def scenario():
        runtime = Runtime(tmp_path, 'rules')
        try:
            request = TurnRequest(session_id='user', turn_id='active', transcript='hello')
            async with runtime.pipeline.turn_scope(request, 'rules'):
                with pytest.raises(ValueError, match='Đợi lượt'):
                    await runtime.pipeline.reset_history('user')
            assert runtime.pipeline._in_scope == 0
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_consent_does_not_override_destination_question_or_negation():
    from src.vivi.dialogue.contracts import PlanDraft, SessionState
    from src.vivi.dialogue.parser import OfflineParser

    parser = OfflineParser()
    session = SessionState(navigation_focus='navigation')
    assert parser.confirmation_reference('Ok, xác nhận.', session) is None
    session.navigation_workspace.draft = PlanDraft(status='preview', offer={'status': 'pending'})
    assert parser.confirmation_reference('Ok, bắt đầu nhưng đừng chạy.', session) is None
    session.navigation_workspace.draft.clarification = {'phase': 'change_destination'}
    assert parser.confirmation_reference('Ok, xác nhận.', session) is None


def test_reset_invalidates_pending_confirmation(tmp_path):
    async def scenario():
        runtime = Runtime(tmp_path, 'rules')
        try:
            response = await runtime.pipeline.run(TurnRequest(session_id='user', turn_id='ask', transcript='Mở cửa ghế phụ'))
            confirmation_id = response.confirmation.confirmation_id
            await runtime.pipeline.reset_history('user')
            response = await runtime.pipeline.run(TurnRequest(session_id='user', turn_id='stale', transcript='Xác nhận',
                                                            confirmation_id=confirmation_id, confirmation_decision='approve'))
            assert not (response.execution or {}).get('executed')
            assert not runtime.vehicle.state_for('user').door_states.front_passenger.open
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_reset_keeps_accepted_journey(tmp_path):
    async def scenario():
        runtime = Runtime(tmp_path, 'rules')
        try:
            await runtime.execute({'type': 'turn', 'id': 'plan', 'query': 'Chỉ đường tới Long Biên.',
                                   'position': {'lat': 21, 'lng': 105.8, 'simulated': True}}, 'user', {})
            await runtime.execute({'type': 'turn', 'id': 'place', 'query': 'Đúng rồi'}, 'user', {})
            started = await runtime.execute({'type': 'turn', 'id': 'start', 'query': 'Ok, bắt đầu.'}, 'user', {})
            assert started['navigation']['status'] == 'running'
            before = await runtime.graph.snapshot('user')
            await runtime.pipeline.reset_history('user')
            after = await runtime.graph.snapshot('user')
            assert after['navigation_workspace']['journey'] == before['navigation_workspace']['journey']
            assert after['vehicle_state'] == before['vehicle_state']
            assert after['snapshot_sequence'] > before['snapshot_sequence']
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_history_reset_endpoint(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType, SimpleNamespace

    import httpx
    from fastapi import FastAPI

    async def scenario():
        runtime = Runtime(tmp_path, 'rules')
        try:
            # Load this route against the isolated runtime, without booting the real car/server.
            fake = ModuleType('src.vivi.api.runtime')
            fake.runtime = SimpleNamespace(orchestrator=runtime.pipeline)
            spec = importlib.util.spec_from_file_location('history_reset_test_route',
                                                        Path('src/vivi/api/routes/history.py'))
            route = importlib.util.module_from_spec(spec)
            with monkeypatch.context() as patch:
                patch.setitem(sys.modules, 'src.vivi.api.runtime', fake)
                spec.loader.exec_module(route)
            app = FastAPI()
            app.include_router(route.router)
            request = TurnRequest(session_id='user', turn_id='old', transcript='Xin chào')
            await runtime.pipeline.run(request)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                async with runtime.pipeline.turn_scope(request.model_copy(update={'turn_id': 'busy'}), 'rules'):
                    response = await client.post('/api/v1/history/reset', json={'session_id': 'user'})
                    assert response.status_code == 409
                response = await client.post('/api/v1/history/reset', json={'session_id': 'user'})
                assert response.status_code == 200
                assert response.headers['cache-control'] == 'no-store'
                assert response.json()['deleted_turns'] == 1
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_cancelled_reset_keeps_admission_closed_until_erasure_finishes(tmp_path, monkeypatch):
    import threading

    async def scenario():
        runtime = Runtime(tmp_path, 'rules')
        entered, release = threading.Event(), threading.Event()
        try:
            request = TurnRequest(session_id='user', turn_id='old', transcript='Xin chào')
            await runtime.pipeline.run(request)
            history = runtime.graph.navigation.workflow.services.history
            original = history.clear

            def blocked_clear(*args):
                entered.set()
                assert release.wait(5)
                return original(*args)

            monkeypatch.setattr(history, 'clear', blocked_clear)
            resetting = asyncio.create_task(runtime.pipeline.reset_history('user'))
            assert await asyncio.to_thread(entered.wait, 3)
            resetting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await resetting
            with pytest.raises(ValueError, match='Đang xoá'):
                await runtime.pipeline.run(request.model_copy(update={'turn_id': 'during-reset'}))
            release.set()
            await runtime.pipeline._reset_task
            assert not runtime.pipeline._resetting
            assert history.recent('user', 6) == []
        finally:
            release.set()
            await runtime.pipeline.aclose()
    asyncio.run(scenario())
