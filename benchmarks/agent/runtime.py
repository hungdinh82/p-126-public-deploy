"""Real agent graph and temporary state, with model-only live access."""

import copy
from types import SimpleNamespace

from fixtures.core import Harness, ScriptedModel
from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.domain.models import VehicleState
from src.vivi.graphs.assistant import build_assistant_graph
from src.vivi.model_gateway import ModelProvider


class ProposalFixture(ScriptedModel):
    tools_as_json = True
    local_semantic_contract = "operations_v1"

    def __init__(self):
        super().__init__()
        self.pending = []
        self.error = False
        self.unexpected = []

    def prepare(self, turn):
        self.pending = copy.deepcopy(turn.fixture_attempts)
        self.error = turn.fixture_error
        self.unexpected = []

    async def agenerate_proposals(self, **kwargs):
        self.calls.append({"stage": "proposal"})
        if self.error:
            raise RuntimeError("Authored benchmark provider outage")
        if not self.pending:
            self.unexpected.append("Agent requested an unauthored inference")
            raise ValueError(self.unexpected[-1])
        return [(call.name, call.arguments) for call in self.pending.pop(0)]


class UnavailableInformation:
    """Expose information tools while testing a failed downstream dependency."""

    online = True
    settings = SimpleNamespace(information_timezone="Asia/Ho_Chi_Minh")

    async def execute(self, *_args, **_kwargs):
        return {"status": "blocked", "message": "Benchmark information dependency unavailable.",
                "error_code": "information_unavailable"}


class UnavailableAnswer:
    async def agenerate_json(self, **_kwargs):
        raise RuntimeError("Authored benchmark answer service unavailable")


def merge(base, patch):
    result = copy.deepcopy(base)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def changes(before, after, prefix=""):
    result = []
    for key in sorted(before.keys() | after.keys()):
        path = f"{prefix}.{key}" if prefix else key
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            result.extend(changes(old, new, path))
        elif old != new:
            result.append(path)
    return result


def proposal_calls(attempts):
    batches = [p for a in attempts for p in a.get("proposal_attempts", [])]
    def normalize(calls):
        return [{"name": name, "arguments": args} for name, args in calls]
    return {
        "first_calls": normalize(batches[0].get("calls", [])) if batches else [],
        "tool_calls": normalize(batches[-1].get("calls", [])) if batches else [],
    }


class AgentRuntime:
    def __init__(self, root, case, client=None, config=None, provider="local"):
        self.fixture = ProposalFixture() if client is None else None
        self.client = client or self.fixture
        self.harness = Harness(root, model=self.client)
        if config is not None:
            self.harness.graph = build_assistant_graph(
                self.harness.services, self.harness.saver, self.harness.ledger,
                resolver=SemanticResolver(
                    gateway=self.harness.models, budget_seconds=config.semantic_budget_seconds,
                ),
            )
        self.harness.services.information = UnavailableInformation()
        from src.vivi.conversation.service import ConversationService

        self.harness.services.conversation = ConversationService(
            {provider: ModelProvider(provider, UnavailableAnswer(), 2)},
            fallback_chains={provider: [provider]},
        )
        if case.dependencies == "synthetic":
            from benchmarks.agent.dependencies import attach

            attach(self.harness.services, root, provider)
        initial = merge(VehicleState().model_dump(mode="json"), case.initial_vehicle)
        self.harness.vehicle.apply_shared_fixture(VehicleState.model_validate(initial))
        budget = config.local_budget_seconds if config and provider == "local" else config.cloud_budget_seconds if config else 2
        # Resolver and gateway share this dictionary. Replacing it leaves the
        # resolver's literal-answer path looking at stale provider objects.
        self.harness.models.providers.clear()
        self.harness.models.providers[provider] = ModelProvider(provider, self.client, budget)
        self.harness.models.primary = provider
        self.harness.models.fallback_chains = {provider: [provider]}
        # Harness's resolver references this gateway; no production runtime is used.
        self.harness.graph.default_provider = provider
        self.provider = provider
        self.timeout = config.turn_timeout_seconds if config else 10
        self.number = 0

    async def turn(self, turn):
        import asyncio

        self.number += 1
        if self.fixture:
            self.fixture.prepare(turn)
        before = self.harness.vehicle.state_for("driver").model_dump(mode="json")
        result = await asyncio.wait_for(self.harness.graph.ainvoke({
            "session_id": "driver", "turn_id": str(self.number), "input_text": turn.text,
            "position": {"lat": 21, "lng": 105.85},
        }), self.timeout)
        after = self.harness.vehicle.state_for("driver").model_dump(mode="json")
        output = result["output"]
        attempts = output["diagnostics"].get("provider_attempts", [])
        changed = changes(before, after)
        actual = {
            "output": output,
            "semantic": output["diagnostics"].get("semantic"),
            "steps": result.get("steps", []),
            "vehicle_before": before, "vehicle": after,
            "vehicle_unchanged": before == after,
            "changed_paths": changed,
            "attempts": attempts,
            "model_calls": sum(a.get("calls", 0) for a in attempts),
            "repairs": sum(a.get("repairs", 0) for a in attempts),
            "fallback": output["diagnostics"].get("semantic_source") == "fallback",
            "fixture_errors": self.fixture.unexpected if self.fixture else [],
            "unused_fixture_attempts": len(self.fixture.pending) if self.fixture else 0,
            **proposal_calls(attempts),
        }
        return actual

    async def close(self):
        await self.harness.close()


def live_resolver(provider, config):
    from eval.semantic_benchmark import GENERATORS

    client = GENERATORS[provider](config).client
    client.max_attempts = 1
    # Every provider is evaluated on the same well-defined operation tools; the
    # cloud client keeps native function calling instead of the JSON grammar.
    client.local_semantic_contract = "operations_v1"
    if provider != "local":
        client.tools_as_json = False
    client.max_tokens = min(config.planner_max_tokens, config.local_llm_max_tokens) if provider == "local" else config.planner_max_tokens
    budget = config.local_budget_seconds if provider == "local" else config.cloud_budget_seconds
    from src.vivi.model_gateway import ModelGateway

    gateway = ModelGateway({provider: ModelProvider(provider, client, budget)}, primary=provider,
                           fallback_chains={provider: [provider]}, breaker_seconds=0)
    return client, SemanticResolver(gateway=gateway, budget_seconds=config.semantic_budget_seconds)
