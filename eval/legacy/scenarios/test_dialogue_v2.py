"""Unified workflow: semantic equivalence, availability and durable effects."""
import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.config import Settings
from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from eval.legacy.grammar import OfflineParser
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.dialogue.reducer import reduce_turn
from src.vivi.domain.models import TurnRequest
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.orchestration import LangGraphOrchestrator
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.event_store import EventStore
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.support.dialogue_v2 import ScriptedModel as ScriptedModel
from tests.support.dialogue_v2 import build as build
from tests.support.dialogue_v2 import invoke as invoke
from tests.support.dialogue_v2 import services as services


def interpretation(capability, arguments=None, act="request", **extra):
    return {"act": act, "actions": [{"capability": capability, "arguments": arguments or {}}], **extra}






@pytest.mark.asyncio
async def test_correction_keeps_zone_and_invalidates_old_confirmation(services, tmp_path):
    graph, store, gateway = build(services, tmp_path)
    first = await invoke(graph, "mở kính bên tài", "one")
    old = first["output"]["confirmation"]["confirmation_id"]
    second = await invoke(graph, "mở một nửa thôi", "two")
    assert second["output"]["action_proposal"]["arguments"] == {"zone": "driver", "position_percent": 50}
    assert second["session"]["active"]["revision"] == 2
    assert first["session"]["active"]["id"] == second["session"]["active"]["id"]
    assert gateway.resolve_confirmation("driver", "old", old, "approve")[1].status == "denied"
    assert (await graph.compiled.aget_state(graph.config(graph.event({"session_id": "driver", "turn_id": "two", "input_text": "x"})))).next == ("await_input",)
    graph.close()
    graph, _, _ = build(services, tmp_path, store=store)
    final = await invoke(graph, "đúng rồi", "three")
    assert final["execution"]["verified"] and final["vehicle_state"]["window_positions"]["driver"] == 50
    assert final["session"]["active"]["steps"][0]["status"] == "completed"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM dialogue_sessions").fetchone()[0] == 0  # Checkpoint owns tasks.
    graph.close()


@pytest.mark.asyncio
async def test_capability_change_keeps_only_declared_compatible_fields(services, tmp_path):
    graph, _, _ = build(services, tmp_path)
    await invoke(graph, "mở cửa trước bên phải", "one")
    corrected = await invoke(graph, "không, mở cửa kính bên đấy thôi", "two")
    assert corrected["action_proposal"]["arguments"] == {"zone": "front_passenger", "position_percent": 100}
    final = await invoke(graph, "ừ đúng rồi", "three")
    assert final["vehicle_state"]["window_positions"]["front_passenger"] == 100
    assert not final["vehicle_state"]["door_states"]["front_passenger"]["open"]
    graph.close()


@pytest.mark.asyncio
async def test_exclusion_never_affects_driver(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    from src.vivi.domain.models import ActionProposal
    gateway.vehicle.execute_sync("driver", "seed", ActionProposal(intent="window.set_position", arguments={"zone": "all", "position_percent": 100}))
    first = await invoke(graph, "đóng tất cả cửa sổ trừ cửa ghế lái", "one")
    assert first["action_proposal"]["arguments"]["zones"] == ["front_passenger", "rear_left", "rear_right"]
    assert first["status"] == "action_verified"  # Closing is R1, as defined by vehicle policy.
    assert first["vehicle_state"]["window_positions"] == {"driver": 100, "front_passenger": 0, "rear_left": 0, "rear_right": 0}
    graph.close()


@pytest.mark.asyncio
async def test_chain_stops_for_slot_and_confirmation_then_completes(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "đặt nhiệt độ 24 độ rồi mở cửa rồi phát nhạc", "one")
    assert [s["status"] for s in first["steps"]] == ["completed", "waiting_slot", "pending"]
    assert first["status"] == "clarify" and not gateway.vehicle.state_for("driver").media_playing
    second = await invoke(graph, "phía trước bên phải", "two")
    assert second["status"] == "confirmation_required"
    final = await invoke(graph, "xác nhận", "three")
    assert all(s["status"] == "completed" for s in final["steps"])
    assert gateway.vehicle.state_for("driver").media_playing
    assert gateway.vehicle.state_for("driver").state_version == 3
    graph.close()


@pytest.mark.asyncio
async def test_cancellation_preserves_completed_steps(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    await invoke(graph, "đặt nhiệt độ 24 độ rồi mở cửa bên tài rồi phát nhạc", "one")
    cancelled = await invoke(graph, "huỷ", "two")
    assert [s["status"] for s in cancelled["steps"]] == ["completed", "cancelled", "cancelled"]
    assert gateway.vehicle.state_for("driver").temperature_celsius == 24
    assert not gateway.vehicle.state_for("driver").media_playing
    graph.close()


@pytest.mark.asyncio
async def test_suspend_question_restart_and_resume_requires_fresh_grant(services, tmp_path):
    graph, store, gateway = build(services, tmp_path)
    first = await invoke(graph, "mở kính lái", "one")
    old = first["confirmation"]["confirmation_id"]
    await invoke(graph, "tạm giữ", "two")
    await invoke(graph, "chào", "three")
    graph.close()
    graph, _, _ = build(services, tmp_path, store=store)
    resumed = await invoke(graph, "tiếp tục", "four")
    assert resumed["status"] == "confirmation_required" and resumed["confirmation"]["confirmation_id"] != old
    assert gateway.resolve_confirmation("driver", "old", old, "approve")[1].status == "denied"
    graph.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_reply", [{"act": "clarify", "text": "Bạn nói rõ nhé."}, {"act": "unsupported", "text": "Chưa hỗ trợ."}])
async def test_valid_semantics_never_fall_back(services, tmp_path, model_reply):
    local = ScriptedModel([])
    graph, _, gateway = build(services, tmp_path, ScriptedModel([model_reply]), local=local)
    result = await invoke(graph, "đặt nhiệt độ 25 độ", "one")
    assert result["diagnostics"]["effective_provider"] == "google"
    assert not local.prompts and gateway.vehicle.state_for("driver").state_version == 0
    graph.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud,local,served", [(RuntimeError("offline"), interpretation("media.play"), "local"), (RuntimeError("offline"), RuntimeError("offline"), "rules"), (RuntimeError("offline"), None, "rules")])
async def test_fallback_chain(services, tmp_path, cloud, local, served):
    graph, _, gateway = build(services, tmp_path, ScriptedModel([cloud]), local=ScriptedModel([local]) if local is not None else None)
    result = await invoke(graph, "phát nhạc", "one")
    assert result["diagnostics"]["effective_provider"] == served and gateway.vehicle.state_for("driver").media_playing
    assert result["diagnostics"]["provider_attempts"][0]["outcome"] == "fallback"
    graph.close()


@pytest.mark.asyncio
async def test_schema_repair_and_circuit_recovery():
    client = ScriptedModel(["broken JSON", {"act": "reply", "text": "Có nhé."}, RuntimeError("offline"), {"act": "reply", "text": "Đã phục hồi."}])
    resolver = SemanticResolver({"google": ModelProvider("google", client, .1)}, breaker_seconds=.01)
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[1] == "google"
    assert "Repair" in client.prompts[1]
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[1] == "rules"
    before = len(client.prompts)
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[2][0]["outcome"] == "circuit_open"
    assert len(client.prompts) == before
    await asyncio.sleep(.015)
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[1] == "google"


@pytest.mark.asyncio
async def test_local_tool_semantic_contract():
    class Local:
        async def agenerate_tool(self, **kwargs):
            assert kwargs["tools"][0]["function"]["name"] == "interpret_turn"
            return "interpret_turn", {"act": "reply", "text": "Mình đây."}
    resolver = SemanticResolver({"local": ModelProvider("local", Local(), .1)})
    value, served, _ = await resolver.resolve("chào", SessionState(), requested="local")
    assert served == "local" and value.act == "reply"


@pytest.mark.asyncio
async def test_timeout_moves_to_parser():
    class Slow:
        async def agenerate_json(self, **kwargs):
            await asyncio.sleep(30)
    resolver = SemanticResolver({"google": ModelProvider("google", Slow(), .01)})
    value, served, attempts = await resolver.resolve("phát nhạc", SessionState(), requested="google")
    assert served == "rules" and value.actions[0].capability == "media.play"
    assert attempts[0]["reason"] == "TimeoutError"


@pytest.mark.asyncio
async def test_duplicate_turn_calls_model_and_executor_once(services, tmp_path):
    model = ScriptedModel([interpretation("climate.set_temperature", {"value_celsius": 25})])
    graph, store, gateway = build(services, tmp_path, model)
    orchestrator = LangGraphOrchestrator({"google": graph}, gateway, EventStore(Settings(data_dir=tmp_path)), ledger=store)
    request = TurnRequest(transcript="đặt 25 độ", session_id="driver", turn_id="one")
    first, repeat = await asyncio.gather(orchestrator.run(request, "google"), orchestrator.run(request, "google"))
    assert first.model_dump() == repeat.model_dump() and len(model.prompts) == 1
    assert gateway.vehicle.state_for("driver").state_version == 1
    with pytest.raises(ValueError):
        await orchestrator.run(request.model_copy(update={"transcript": "đặt 26 độ"}), "google")
    graph.close()


@pytest.mark.asyncio
async def test_cancel_during_model_never_dispatches(services, tmp_path):
    entered = asyncio.Event()
    class Slow:
        async def agenerate_json(self, **kwargs):
            entered.set()
            await asyncio.sleep(30)
    graph, store, gateway = build(services, tmp_path, Slow())
    orchestrator = LangGraphOrchestrator({"google": graph}, gateway, EventStore(Settings(data_dir=tmp_path)), ledger=store)
    running = asyncio.create_task(orchestrator.run(TurnRequest(transcript="đặt 25 độ", session_id="driver", turn_id="one"), "google"))
    await asyncio.wait_for(entered.wait(), 2)
    assert orchestrator.cancel("driver", "one") == {"cancelled": True, "dispatched": False}
    with pytest.raises(asyncio.CancelledError):
        await running
    assert gateway.vehicle.state_for("driver").state_version == 0
    graph.close()


@pytest.mark.asyncio
async def test_memory_reset_grant_survives_restart(services, tmp_path):
    from src.vivi.memory.sqlite import MemoryValue
    services.memory.remember("default", MemoryValue(key="display_name", kind="fact", value="Nam"))
    graph, store, _ = build(services, tmp_path)
    proposal = await invoke(graph, "xoá toàn bộ bộ nhớ", "one")
    assert proposal["status"] == "confirmation_required" and services.memory.list("default")
    graph.close()
    graph, _, _ = build(services, tmp_path, store=store)
    final = await graph.ainvoke({"session_id": "driver", "turn_id": "two", "input_text": "xác nhận", "confirmation_id": proposal["confirmation"]["confirmation_id"], "confirmation_decision": "approve"})
    assert final["status"] == "answered" and not services.memory.list("default")
    graph.close()


@pytest.mark.parametrize("text", ["mở cửa bên tài và xoay ghế", "mở kính bên tài rồi bật đèn", "đặt 24 độ rồi phát nhạc rồi mở cửa rồi đóng cốp rồi tắt nhạc rồi mở capo"])
def test_entire_control_must_be_understood_before_any_dispatch(text):
    frame = OfflineParser().interpret(text, SessionState())
    assert frame.act == "clarify" and not frame.actions


@pytest.mark.parametrize("text", ["đừng mở cửa bên tài", "nếu nóng thì mở cửa kính bên tài", "lát nữa mở cốp", "tôi nóng quá tăng điều hoà tối đa"])
def test_constraints_cannot_create_executable_task(text):
    semantic = OfflineParser().interpret(text, SessionState())
    state, _ = reduce_turn(SessionState(), semantic)
    assert state.active is None


def test_model_and_parser_semantic_equivalence():
    semantic = OfflineParser().interpret("mở kính bên tài", SessionState())
    model = SemanticTurn.model_validate(json.loads(semantic.model_dump_json()))
    first, _ = reduce_turn(SessionState(), semantic)
    second, _ = reduce_turn(SessionState(), model)
    assert first.active.steps[0].draft == second.active.steps[0].draft
    from src.vivi.capabilities.registry import REGISTRY
    assert REGISTRY[semantic.actions[0].capability].validate(first.active.steps[0].draft) == REGISTRY[model.actions[0].capability].validate(second.active.steps[0].draft)


def test_expired_or_completed_task_does_not_authorize_confirmation():
    session = SessionState(active=DialogueTask(expires_at=datetime.now(UTC) - timedelta(seconds=1), steps=[TaskStep(draft=ActionDraft(capability="media.play"), status="waiting_confirmation")]))
    state, message = reduce_turn(session, SemanticTurn(act="confirm", task_ref="active"))
    assert state.active is None and message


def test_schema_closes_every_nested_object():
    from src.vivi.structured_llm import strict_schema
    schema = strict_schema(SemanticTurn.model_json_schema())
    def check(value):
        if isinstance(value, dict):
            if "properties" in value:
                assert value["additionalProperties"] is False and set(value["required"]) == set(value["properties"])
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(schema)


@pytest.mark.asyncio
async def test_stream_carries_provider_steps_and_replays_one_final(services, tmp_path):
    graph, store, gateway = build(services, tmp_path)
    orchestrator = LangGraphOrchestrator({"rules": graph}, gateway, EventStore(Settings(data_dir=tmp_path)), ledger=store)
    request = TurnRequest(session_id="driver", turn_id="one", transcript="đặt nhiệt độ 24 độ rồi phát nhạc")
    events = [e async for e in orchestrator.run_stream(request)]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert len([e for e in events if e["type"] == "step"]) == 2
    assert any(e["type"] == "provider" and e["provider"] == "rules" for e in events)
    replay = [e async for e in orchestrator.run_stream(request)]
    assert len(replay) == 1 and replay[0]["response"] == events[-1]["response"]
    graph.close()
def test_state_changed_after_consent_blocks_dispatch(services, tmp_path):
    from src.vivi.agents.contracts import ActionProposal
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    gateway = VehicleActionGateway(services.action_gateway.vehicle, DurableConfirmations(store), ledger=store)
    proposal = ActionProposal(intent="window.set_position", arguments={"zone": "driver", "position_percent": 100})
    grant = gateway.check("driver", proposal, turn_id="offer").confirmation
    assert gateway.resolve_confirmation("driver", "approve", grant["confirmation_id"], "approve")[1].allowed
    services.action_gateway.vehicle.set_driving("driver", True)
    result = gateway.execute("driver", "approve", proposal)
    assert not result.executed
    assert "stale" in result.error
    assert services.action_gateway.vehicle.state_for("driver").window_driver_percent == 0

def test_interrupted_dispatch_is_not_reissued(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    payload = {"provider": "google", "transcript": "mở kính lái"}
    action = {"intent": "window.set_position", "arguments": {"zone": "driver", "position_percent": 100}}
    store.reserve_turn("default:driver", "one", payload)
    store.reserve_command("driver", "one", action)
    store.fail_turn("default:driver", "one")
    reopened = RuntimeStore(tmp_path / "runtime.sqlite3")
    status = reopened.turn_status("default:driver", "one", "driver")
    assert status["status"] == "interrupted" and status["command"]["status"] == "dispatched"
    with pytest.raises(ValueError, match="reconcile"):
        reopened.reserve_command("driver", "one", action)

def test_adapter_rejects_changed_version_atomically(services):
    from src.vivi.domain.models import ActionProposal
    vehicle = services.action_gateway.vehicle
    previous = vehicle.state_for("driver").state_version
    vehicle.set_driving("driver", True)
    with pytest.raises(ValueError, match="state_version_conflict"):
        vehicle.execute_sync("driver", "one", ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": 26}), expected_state_version=previous)
    assert vehicle.state_for("driver").temperature_celsius == 23

def test_state_change_between_grant_and_policy_cannot_reauthorize(services, tmp_path):
    from src.vivi.agents.contracts import ActionProposal
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    confirmations = DurableConfirmations(store)
    vehicle = services.action_gateway.vehicle
    gateway = VehicleActionGateway(vehicle, confirmations, ledger=store)
    proposal = ActionProposal(intent="window.set_position", arguments={"zone": "driver", "position_percent": 100})
    grant = gateway.check("driver", proposal, turn_id="offer").confirmation
    original = confirmations.resolve
    def change_after_grant(*args):
        result = original(*args)
        from src.vivi.domain.models import ActionProposal as DomainProposal
        vehicle.execute_sync("driver", "concurrent", DomainProposal(intent="climate.set_temperature", arguments={"value_celsius": 26}))
        return result
    confirmations.resolve = change_after_grant
    checked = gateway.resolve_confirmation("driver", "approve", grant["confirmation_id"], "approve")[1]
    assert not checked.allowed and checked.status == "denied"
    assert not gateway.execute("driver", "approve", proposal).executed

@pytest.mark.asyncio
async def test_future_correction_revokes_and_reissues_current_grant(services, tmp_path):
    model = ScriptedModel([
        {"act": "request", "actions": [{"capability": "window.set_position", "arguments": {"zone": "driver", "position_percent": 100}}, {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}}]},
        interpretation("climate.set_temperature", {"value_celsius": 26}, act="modify", task_ref="active", step_index=1),
        {"act": "confirm", "task_ref": "active"},
    ])
    graph, _, gateway = build(services, tmp_path, model)
    first = await invoke(graph, "mở kính bên tài rồi đặt 24 độ", "one")
    old = first["confirmation"]["confirmation_id"]
    edited = await invoke(graph, "bước sau đổi thành 26 độ", "two")
    assert edited["confirmation"]["confirmation_id"] != old
    assert gateway.resolve_confirmation("driver", "stale", old, "approve")[1].status == "denied"
    final = await invoke(graph, "xác nhận", "three")
    assert final["vehicle_state"]["temperature_celsius"] == 26 and all(s["status"] == "completed" for s in final["steps"])
    graph.close()


@pytest.mark.asyncio
async def test_restart_reconciles_relative_effect_before_advancing(services, tmp_path):
    graph, store, gateway = build(services, tmp_path)
    # Simulate losing the checkpoint write immediately after a successful effect.
    from src.vivi.agents.contracts import ActionProposal
    from src.vivi.dialogue.reducer import prepare_step
    session, _ = reduce_turn(SessionState(), SemanticTurn(act="request", actions=[ActionDraft(capability="climate.set_temperature", arguments={"temperature_delta_celsius": 2}), ActionDraft(capability="media.play")]))
    identifier = f"task:{session.active.id}:r1:step:{session.active.steps[0].id}"
    session = prepare_step(session, 0, {"value_celsius": 25}, identifier)
    proposal = ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": 25})
    assert gateway.check("driver", proposal, turn_id=identifier).allowed
    assert gateway.execute("driver", identifier, proposal).verified
    event = graph.event({"session_id": "driver", "turn_id": "crash", "input_text": "x"})
    await graph.compiled.aupdate_state(graph.config(event), {"session": session.model_dump(mode="json")}, as_node="reducer")
    graph.close()
    graph, _, gateway = build(services, tmp_path, store=store)
    result = await invoke(graph, "tiếp tục", "recovery")
    assert gateway.vehicle.state_for("driver").temperature_celsius == 25
    assert gateway.vehicle.state_for("driver").state_version == 2
    assert all(s["status"] == "completed" for s in result["steps"])
    graph.close()


@pytest.mark.asyncio
async def test_unknown_effect_stops_chain_and_cannot_be_modified_into_retry(services, tmp_path):
    graph, store, gateway = build(services, tmp_path)
    original = gateway.execute
    from src.vivi.vehicle.gateway import GatewayExecution
    gateway.execute = lambda *args: GatewayExecution(False, False, "Chưa có acknowledgement.", {}, "timeout")
    result = await invoke(graph, "đặt nhiệt độ 24 độ rồi phát nhạc", "one")
    assert [s["status"] for s in result["steps"]] == ["unknown", "pending"]
    gateway.execute = original
    resumed = await invoke(graph, "tiếp tục", "two")
    assert not resumed.get("execution") and gateway.vehicle.state_for("driver").state_version == 0
    graph.close()


@pytest.mark.asyncio
async def test_model_failure_repairs_once_then_uses_local():
    cloud = ScriptedModel(["bad", "still bad"])
    local = ScriptedModel([{"act": "reply", "text": "Mình đây."}])
    resolver = SemanticResolver({"google": ModelProvider("google", cloud, .1), "local": ModelProvider("local", local, .1)})
    _, served, _ = await resolver.resolve("chào", SessionState(), requested="google")
    assert served == "local" and len(cloud.prompts) == 2


@pytest.mark.asyncio
async def test_generation_fallback_never_reinterprets_or_repeats_vehicle_effect(services, tmp_path):
    from src.vivi.rag.generator import ExtractiveHandbookGenerator
    from tests.support.rag import FakeRetriever
    class FailedGenerator:
        async def agenerate(self, *args):
            raise ConnectionError("generation offline")
    model = ScriptedModel([{"act": "request", "actions": [
        {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}},
        {"capability": "manual.search", "arguments": {"query": "Cách kết nối cáp sạc AC?"}}]}])
    services.retriever = FakeRetriever()
    graph, store, gateway = build(services, tmp_path, model)
    # Build with the same reducer/resolver but independent generation failures.
    graph.close()
    graph = build_assistant_graph(services, ThreadedSqliteSaver(tmp_path / "checkpoints.sqlite3"), store,
        resolver=SemanticResolver({"google": ModelProvider("google", model, .2)}),
        generators={"google": FailedGenerator(), "local": FailedGenerator(), "rules": ExtractiveHandbookGenerator()})
    services.action_gateway = gateway
    result = await graph.ainvoke({"session_id": "driver", "turn_id": "one", "input_text": "đặt 24 độ rồi tra cẩm nang", "provider": "google"})
    assert len(model.prompts) == 1 and gateway.vehicle.state_for("driver").state_version == 1
    assert result["citations"] and result["diagnostics"]["generation_provider"] == "rules"
    assert [a["outcome"] for a in result["diagnostics"]["generation_attempts"]] == ["fallback", "fallback", "served"]
    graph.close()


@pytest.mark.asyncio
async def test_wrong_version_evidence_is_rejected_without_generation(services, tmp_path):
    from tests.support.rag import _chunk
    class WrongVersion:
        def retrieve(self, *args):
            return [_chunk().model_copy(update={"model_year": 2025})]
    services.retriever = WrongVersion()
    graph, _, _ = build(services, tmp_path)
    result = await invoke(graph, "Cách sạc AC?", "one")
    assert result["grounding_status"] == "insufficient_evidence" and not result["citations"]
    assert result["diagnostics"]["generation_attempts"] == []
    graph.close()


def test_migration_backups_and_invalidates_once_without_losing_effects(services, tmp_path):
    from src.vivi.memory.sqlite import MemoryValue
    from src.vivi.persistence.migration import migrate_runtime
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    grants = DurableConfirmations(store)
    with store.connect() as db:
        db.execute("INSERT INTO dialogue_sessions VALUES (?, ?, 1)", ("driver", json.dumps({"legacy": True})))
    store.reserve_command("driver", "old-command", {"intent": "media.play", "arguments": {}})
    services.memory.remember("default", MemoryValue(key="display_name", kind="fact", value="Nam"))
    services.history.save({"session_id": "driver", "turn_id": "history", "query": "Chào", "answer": "Mình đây."})
    grants.create_generic("driver", {"old": True}, "old grant")
    backups = migrate_runtime(store, databases=[services.memory.path, services.history.path])
    assert len(backups) == 3 and all(__import__('pathlib').Path(p).is_file() for p in backups)
    with store.connect() as db:
        assert db.execute("SELECT task FROM dialogue_sessions").fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM suspended_tasks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 1
        assert db.execute("SELECT used FROM confirmation_grants").fetchone()[0] == 1
    assert services.memory.list("default")[0]["value"] == "Nam" and services.history.recent("driver")[0]["answer"] == "Mình đây."
    assert migrate_runtime(store) == []


@pytest.mark.asyncio
async def test_negated_pending_control_revokes_consent(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "mở kính bên tài", "one")
    grant = first["output"]["confirmation"]["confirmation_id"]
    denied = await invoke(graph, "đừng mở cửa sổ", "two")
    assert denied["session"]["active"]["steps"][0]["status"] == "cancelled"
    assert gateway.resolve_confirmation("driver", "old", grant, "approve")[0] is None
    after = await invoke(graph, "có", "three")
    assert not after["output"]["execution"] and gateway.vehicle.state_for("driver").window_driver_percent == 0
    graph.close()


@pytest.mark.parametrize("arguments", [{}, {"zone":"driver"}])
def test_reducer_binds_reference_and_rejects_invented_side(arguments):
    state, _ = reduce_turn(SessionState(), SemanticTurn(act="request", actions=[ActionDraft(capability="door.set_open", arguments={"zone":"front_passenger", "open":True})]))
    modified, message = reduce_turn(state, SemanticTurn(act="modify", task_ref="active", actions=[ActionDraft(capability="window.set_position", arguments={"position_percent":50, **arguments}, referenced_target=True)]))
    if arguments:
        assert message and modified.active.revision == 1
    else:
        assert not message and modified.active.steps[0].draft.arguments.zone == "front_passenger"
        assert modified.active.steps[0].draft.arguments.open is None


@pytest.mark.asyncio
async def test_multiple_exclusions_preserved_across_confirmation(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    first = await invoke(graph, "mở tất cả cửa trừ bên tài và bên phụ", "one")
    assert first["output"]["action_proposal"]["arguments"] == {"zones":["rear_left", "rear_right"], "open":True}
    final = await invoke(graph, "đồng ý", "two")
    state = gateway.vehicle.state_for("driver")
    assert final["output"]["status"] == "action_verified"
    assert not state.door_states.driver.open and not state.door_states.front_passenger.open
    assert state.door_states.rear_left.open and state.door_states.rear_right.open
    graph.close()


@pytest.mark.parametrize("text,capability,args", [
    ("Hạ kính bên phụ xuống 40 phần trăm", "window.set_position", {"zone":"front_passenger", "position_percent":40}),
    ("Làm ơn đặt điều hoà về 21 độ nhé", "climate.set_temperature", {"value_celsius":21}),
    ("Mở cửa kính bên tài và bên phụ", "window.set_position", {"zones":["driver", "front_passenger"], "position_percent":100}),
    ("Khoá cửa sau bên phải giúp mình", "door.set_lock", {"zone":"rear_right", "locked":True}),
])
def test_paraphrases_outside_prompt(text, capability, args):
    frame = OfflineParser().interpret(text, SessionState())
    assert frame.act == "request" and len(frame.actions) == 1
    assert frame.actions[0].capability == capability and frame.actions[0].arguments.model_dump(exclude_none=True) == args


def test_architecture_has_one_builder_and_one_language_boundary():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1] / "src/vivi"
    assert not (root / "agents/graph.py").exists() and not (root / "graphs/shadow.py").exists()
    forbidden = {"classifier", "commands", "context", "dialogue", "planner", "slots", "tasks", "authorization", "native_tools", "state", "voice"}
    for path in root.rglob("*.py"):
        module = ast.parse(path.read_text())
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.vivi.agents."):
                assert node.module.rsplit(".", 1)[-1] not in forbidden, (path, node.module)
    for directory in ("graphs", "capabilities"):
        for path in (root / directory).glob("*.py"):
            assert "normalize_text" not in path.read_text()


@pytest.mark.asyncio
async def test_status_then_control_is_not_swallowed_by_observation_parser(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    out = await invoke(graph, "Kiểm tra tình trạng xe rồi mở cửa bên tài", "one")
    assert [s["capability"] for s in out["output"]["steps"]] == ["vehicle.get_status", "door.set_open"]
    assert [s["status"] for s in out["output"]["steps"]] == ["completed", "waiting_confirmation"]
    assert not gateway.vehicle.state_for("driver").door_driver_open
    graph.close()


@pytest.mark.asyncio
async def test_question_then_unknown_control_dispatches_nothing(services, tmp_path):
    graph, store, _ = build(services, tmp_path)
    out = await invoke(graph, "Kiểm tra pin và mở cửa rồi bật tính năng chưa hỗ trợ", "one")
    assert out["output"]["status"] == "clarify" and out["output"]["steps"] == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 0
    graph.close()


@pytest.mark.asyncio
async def test_resolved_step_is_durable_before_external_effect(services, tmp_path):
    graph, _, gateway = build(services, tmp_path)
    execute = gateway.vehicle.execute_sync
    observations = []
    def inspect_before_effect(session, command, proposal, **options):
        snapshot = graph.compiled.get_state(graph.config(graph.event({"session_id":"driver", "turn_id":"one", "input_text":"Tôi hơi lạnh"})))
        step = snapshot.values["session"]["active"]["steps"][0]
        observations.append((snapshot.next, step["status"], step["resolved_arguments"], step["command_id"]))
        return execute(session, command, proposal, **options)
    gateway.vehicle.execute_sync = inspect_before_effect
    output = await invoke(graph, "Tôi hơi lạnh", "one")
    assert output["status"] == "action_verified" and len(observations) == 1
    next_nodes, status, arguments, command = observations[0]
    assert next_nodes == ("capability",) and status == "running"
    assert arguments == {"value_celsius":25} and command == output["steps"][0]["command_id"]
    graph.close()


@pytest.mark.parametrize("query,model,year", [("Thông số pin VF8 đời 2025?", "VF8", 2025), ("Dung lượng pin VF9 năm 2026?", "VF9", 2026)])
@pytest.mark.asyncio
async def test_offline_requested_edition_never_uses_current_vehicle_evidence(services, tmp_path, query, model, year):
    from tests.support.history_regressions import chunk
    class Retriever:
        def retrieve(self, text, requested_model, requested_year, locale):
            assert (requested_model, requested_year) == (model, year)
            return [chunk("current", "Dung lượng pin 82 kWh.", "Pin VF8 2026")]
    runtime = replace(services, retriever=Retriever())
    graph, _, _ = build(runtime, tmp_path)
    out = await invoke(graph, query, "one")
    assert out["output"]["grounding_status"] == "insufficient_evidence"
    assert out["output"]["citations"] == [] and "82" not in out["output"]["response_text"]
    graph.close()
