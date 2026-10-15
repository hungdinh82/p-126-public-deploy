"""Unified workflow: semantic equivalence, availability and durable effects."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.vivi.dialogue.contracts import ActionDraft, DialogueTask, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.planner import ModelProvider, SemanticResolver
from src.vivi.dialogue.reducer import reduce_turn
from src.vivi.persistence.confirmations import DurableConfirmations
from src.vivi.persistence.runtime_store import RuntimeStore
from src.vivi.vehicle.gateway import VehicleActionGateway
from tests.support.dialogue_v2 import ScriptedModel as ScriptedModel
from tests.support.dialogue_v2 import build as build
from tests.support.dialogue_v2 import invoke as invoke
from tests.support.dialogue_v2 import services as services


def interpretation(capability, arguments=None, act="request", **extra):
    return {"act": act, "actions": [{"capability": capability, "arguments": arguments or {}}], **extra}


@pytest.mark.asyncio
async def test_schema_repair_and_circuit_recovery():
    client = ScriptedModel(
        [
            "broken JSON",
            {"act": "reply", "text": "Có nhé."},
            RuntimeError("offline"),
            {"act": "reply", "text": "Đã phục hồi."},
        ]
    )
    resolver = SemanticResolver({"google": ModelProvider("google", client, 0.1)}, breaker_seconds=0.01)
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[1] == "google"
    assert "Repair" in client.prompts[1]
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[1] is None
    before = len(client.prompts)
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[2][0]["outcome"] == "circuit_open"
    assert len(client.prompts) == before
    await asyncio.sleep(0.015)
    assert (await resolver.resolve("chào", SessionState(), requested="google"))[1] == "google"


@pytest.mark.asyncio
async def test_local_tool_semantic_contract():
    class Local:
        async def agenerate_tool(self, **kwargs):
            assert kwargs["tools"][0]["function"]["name"] == "interpret_turn"
            return "interpret_turn", {"act": "reply", "text": "Mình đây."}

    resolver = SemanticResolver({"local": ModelProvider("local", Local(), 0.1)})
    value, served, _ = await resolver.resolve("chào", SessionState(), requested="local")
    assert served == "local" and value.act == "reply"


@pytest.mark.asyncio
async def test_timeout_does_not_interpret_a_new_command():
    class Slow:
        async def agenerate_json(self, **kwargs):
            await asyncio.sleep(30)

    resolver = SemanticResolver({"google": ModelProvider("google", Slow(), 0.01)})
    value, served, attempts = await resolver.resolve("phát nhạc", SessionState(), requested="google")
    assert served is None and not value.actions and value.act == "reply"
    assert attempts[0]["reason"] == "TimeoutError"


def test_expired_or_completed_task_does_not_authorize_confirmation():
    session = SessionState(
        active=DialogueTask(
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            steps=[TaskStep(draft=ActionDraft(capability="media.play"), status="waiting_confirmation")],
        )
    )
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
        vehicle.execute_sync(
            "driver",
            "one",
            ActionProposal(intent="climate.set_temperature", arguments={"value_celsius": 26}),
            expected_state_version=previous,
        )
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

        vehicle.execute_sync(
            "driver", "concurrent", DomainProposal(intent="climate.set_temperature", arguments={"value_celsius": 26})
        )
        return result

    confirmations.resolve = change_after_grant
    checked = gateway.resolve_confirmation("driver", "approve", grant["confirmation_id"], "approve")[1]
    assert not checked.allowed and checked.status == "denied"
    assert not gateway.execute("driver", "approve", proposal).executed


@pytest.mark.asyncio
async def test_model_failure_repairs_once_then_uses_local():
    cloud = ScriptedModel(["bad", "still bad"])
    local = ScriptedModel([{"act": "reply", "text": "Mình đây."}])
    resolver = SemanticResolver(
        {"google": ModelProvider("google", cloud, 0.1), "local": ModelProvider("local", local, 0.1)}
    )
    _, served, _ = await resolver.resolve("chào", SessionState(), requested="google")
    assert served == "local" and len(cloud.prompts) == 2


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
    assert len(backups) == 3 and all(__import__("pathlib").Path(p).is_file() for p in backups)
    with store.connect() as db:
        assert db.execute("SELECT task FROM dialogue_sessions").fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM suspended_tasks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM command_ledger").fetchone()[0] == 1
        assert db.execute("SELECT used FROM confirmation_grants").fetchone()[0] == 1
    assert (
        services.memory.list("default")[0]["value"] == "Nam"
        and services.history.recent("driver")[0]["answer"] == "Mình đây."
    )
    assert migrate_runtime(store) == []


@pytest.mark.parametrize("arguments", [{}, {"zone": "driver"}])
def test_reducer_binds_reference_and_rejects_invented_side(arguments):
    state, _ = reduce_turn(
        SessionState(),
        SemanticTurn(
            act="request",
            actions=[ActionDraft(capability="door.set_open", arguments={"zone": "front_passenger", "open": True})],
        ),
    )
    modified, message = reduce_turn(
        state,
        SemanticTurn(
            act="modify",
            task_ref="active",
            actions=[
                ActionDraft(
                    capability="window.set_position",
                    arguments={"position_percent": 50, **arguments},
                    referenced_target=True,
                )
            ],
        ),
    )
    if arguments:
        assert message and modified.active.revision == 1
    else:
        assert not message and modified.active.steps[0].draft.arguments.zone == "front_passenger"
        assert modified.active.steps[0].draft.arguments.open is None


def test_architecture_has_one_builder_and_one_language_boundary():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "src/vivi"
    assert not (root / "agents/graph.py").exists() and not (root / "graphs/shadow.py").exists()
    forbidden = {
        "classifier",
        "commands",
        "context",
        "dialogue",
        "planner",
        "slots",
        "tasks",
        "authorization",
        "native_tools",
        "state",
        "voice",
    }
    for path in root.rglob("*.py"):
        module = ast.parse(path.read_text())
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.vivi.agents."):
                assert node.module.rsplit(".", 1)[-1] not in forbidden, (path, node.module)
    for directory in ("graphs", "capabilities"):
        for path in (root / directory).glob("*.py"):
            assert "normalize_text" not in path.read_text()
