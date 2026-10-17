import asyncio
import time

import pytest

from src.vivi.dialogue.contracts import SessionState
from src.vivi.dialogue.planner import SemanticResolver
from src.vivi.model_gateway import ModelGateway, ModelProvider
from src.vivi.turn_control import TurnControl, current_control
from tests.support.core import ScriptedModel, action


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["đi Tràng An", "dẫn đường đến Tràng An"])
async def test_natural_language_always_calls_model(utterance):
    model = ScriptedModel([action("navigation.plan", {"query": "Tràng An"})])
    resolver = SemanticResolver(gateway=ModelGateway({"google": ModelProvider("google", model)}))
    result, provider, attempts = await resolver.resolve(utterance, SessionState(), requested="google")
    assert result.actions[0].arguments.query == "Tràng An"
    assert provider == "google"
    assert len(model.calls) == 1 and attempts[0]["calls"] == 1


@pytest.mark.asyncio
async def test_valid_clarification_does_not_fall_through_to_another_model():
    cloud = ScriptedModel([{"act": "clarify", "text": "Bạn nói rõ thêm nhé."}])
    local = ScriptedModel([action("media.play")])
    resolver = SemanticResolver(
        gateway=ModelGateway({"google": ModelProvider("google", cloud), "local": ModelProvider("local", local)})
    )
    result, provider, _ = await resolver.resolve("yêu cầu mơ hồ", SessionState())
    assert result.act == "clarify" and provider == "google"
    assert not local.calls


@pytest.mark.asyncio
async def test_model_slots_are_not_rewritten_by_parser():
    model = ScriptedModel([action("climate.set_temperature", {"value_celsius": 25})])
    resolver = SemanticResolver(gateway=ModelGateway({"google": ModelProvider("google", model)}))
    result, _, _ = await resolver.resolve("đặt nhiệt độ 25", SessionState())
    assert result.actions[0].arguments.value_celsius == 25


@pytest.mark.asyncio
async def test_global_deadline_bounds_fallback_and_repair():
    class Slow:
        calls = 0

        async def agenerate_json(self, **_):
            self.calls += 1
            await asyncio.sleep(1)

    model = Slow()
    control = TurnControl(time.monotonic() + 0.02)
    token = current_control.set(control)
    try:
        resolver = SemanticResolver(
            gateway=ModelGateway({"google": ModelProvider("google", model), "local": ModelProvider("local", model)})
        )
        started = time.monotonic()
        result, provider, attempts = await resolver.resolve("mở cửa và bật nhạc", SessionState())
        assert time.monotonic() - started < 0.2
        assert provider is None and not result.actions and 1 <= model.calls <= 2
        assert attempts[-1]["source"] == "fallback"
    finally:
        current_control.reset(token)


@pytest.mark.asyncio
async def test_missing_plan_purpose_in_chain_clarifies_without_guess_or_provider_fallback():
    cloud = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [
                    {"capability": "navigation.plan", "arguments": {"query": "Cần Thơ", "trip_kind": "round_trip"}},
                    {"capability": "media.play"},
                ],
            }
        ]
    )
    local = ScriptedModel([action("media.play")])
    resolver = SemanticResolver(
        gateway=ModelGateway({"google": ModelProvider("google", cloud), "local": ModelProvider("local", local)})
    )
    result, provider, _ = await resolver.resolve("tính kế hoạch khứ hồi rồi bật nhạc", SessionState())
    assert provider == "google" and result.act == "clarify"
    assert not result.actions and not local.calls
    assert len(cloud.calls) == 1


@pytest.mark.parametrize("text,stop", [("đúng", False), ("đừng", False), ("dừng", True), ("dừng lại", True)])
def test_fallback_preserves_vietnamese_accents_for_stop_and_yes(text, stop):
    from src.vivi.dialogue.fallback import LimitedFallback

    result = LimitedFallback().interpret(text, SessionState())
    assert bool(result.actions) == stop
    if stop:
        assert result.actions[0].capability == "navigation.stop"


@pytest.mark.asyncio
async def test_schema_diagnostics_name_the_invariant_without_input_values():
    malformed = {"act": "reply", "actions": [{"capability": "media.play"}], "text": "private user content"}
    model = ScriptedModel([malformed, malformed])
    resolver = SemanticResolver(gateway=ModelGateway({"google": ModelProvider("google", model)}))
    _, provider, attempts = await resolver.resolve("yêu cầu", SessionState())
    assert provider is None
    error = attempts[0]["schema_errors"][0]
    assert error["code"] == "value_error"
    assert "action drafts" in error["message"]
    assert "private user content" not in str(attempts)
    assert "Invalid output to repair" in model.calls[1]["user"]
    assert "private user content" in model.calls[1]["user"]
