"""All 44 audited utterances in order; provider mocks measure contracts, not NLU."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.vivi.dialogue.contracts import DialogueTask, SessionState, TaskStep
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.text import normalize_text
from tests.test_dialogue_v2 import build
from tests.test_long_term_memory import services as services
from tests.test_navigation_workflow import Maps


class AuditMaps(Maps):
    async def search(self, text, **kwargs):
        from src.vivi.navigation.vietmap import Place

        # Vendor queries keep Vietnamese spelling ("đà nẵng"); match on the normalized form.
        if normalize_text(text) == "da nang":
            # A city answers as an administrative result with its boundary, as VietMap does.
            return [
                Place(
                    ref_id="dn-test",
                    name="Đà Nẵng",
                    lat=16,
                    lng=108,
                    boundaries=[{"name": "Đà Nẵng", "full_name": "Thành phố Đà Nẵng"}],
                )
            ]
        if "trạm sạc" in text:
            return [Place(ref_id="charger-test", name="Trạm sạc VinFast TEST", lat=21, lng=105.9)]
        return await super().search(text, **kwargs)


class SemanticContractMock:
    async def agenerate_json(self, **kwargs):
        data = json.loads(kwargs["user"])

        def task(value):
            if not value:
                return None
            return DialogueTask(
                steps=[
                    TaskStep(
                        draft={
                            "capability": s["capability"],
                            "arguments": s["arguments"],
                            "excluded_zones": s["excluded_zones"],
                            "exclusion_mode": s["exclusion_mode"],
                        },
                        status=s["status"],
                        missing_slots=s["missing_slots"],
                        result={"message": s.get("question")},
                    )
                    for s in value["steps"]
                ]
            )

        context = data["tasks"]
        session = SessionState(
            active=task(context["active"]),
            suspended=[task(t) for t in context["suspended"]],
            navigation=context["navigation"],
            clarification=context["clarification"],
        )
        return (
            OfflineParser()
            .interpret(data["utterance"], session, data["explicit_user_memory"], data["recent_turns"])
            .model_dump_json()
        )


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_all_latest_logged_turns(services, tmp_path, tier):
    services = replace(services, navigation=AuditMaps())
    model = SemanticContractMock()
    graph, _, _ = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    rows = [
        json.loads(line) for line in (Path(__file__).parents[1] / "tests/fixtures/dialogue_audits/audit_854_897.jsonl").read_text().splitlines()
    ]
    assert [r["row"] for r in rows] == list(range(854, 898))
    for item in rows:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": str(item["row"]),
                "input_text": item["query"],
                "position": {"lat": 21, "lng": 105.8, "simulated": True},
            }
        )
        row = item["row"]
        assert result["response_text"].strip(), row
        # Consent replies and list choices are bound to the open question by rules,
        # never sent to a model (SemanticResolver.resolve).
        bound = any(
            a.get("source") in {"pending_confirmation", "candidate_reference"}
            for a in result["diagnostics"].get("provider_attempts", [])
        )
        assert result["diagnostics"]["effective_provider"] == ("rules" if bound else tier), row
        if row in {855, 856, 881, 882, 883, 896, 897}:
            assert result["status"] == "clarify", (row, result["output"])
            assert not (result.get("execution") or {}).get("executed"), row
        if row in {857, 859, 861, 884}:
            assert result["status"] == "confirmation_required", (row, result["output"])
        if row == 859:
            assert result["action_proposal"]["arguments"] == {"zone": "front_passenger", "position_percent": 100}
        if row == 862:
            assert result["vehicle_state"]["window_positions"]["rear_right"] == 100
        if row in {863, 864, 865, 866, 867, 868, 869}:
            state = await services.action_gateway.vehicle.get_state("driver")
            assert all(level == 0 for level in state.seat_heat_levels.model_dump().values()), row
        if row == 865:
            assert result["steps"][0]["status"] == "offer" and result["steps"][0]["capability"] == "navigation.search"
        if row in {866, 867, 868, 869, 879, 880}:
            assert result["navigation"]["candidates"] and all(
                "Logistics" not in p["name"] for p in result["navigation"]["candidates"]
            ), row
        if row == 874:
            assert result["action_proposal"]["arguments"]["zone"] == "all", (row, result["output"])
        if row == 879:
            semantic = result["diagnostics"]["semantic"]
            assert semantic["actions"][0]["capability"] == "navigation.plan"
            assert normalize_text(semantic["actions"][0]["arguments"]["query"]) == "da nang"
            # A verified city proposes its representative point and asks whether to pick a specific place.
            assert result["navigation"]["selected"]["representative"]
            assert "điểm đại diện Đà Nẵng" in result["response_text"] and result["status"] == "clarify"
            assert not (await services.action_gateway.vehicle.get_state("driver")).driving
        if row in {875, 876}:
            assert "driver" not in result["action_proposal"]["arguments"]["zones"], row
        if row == 877:
            assert all(step["status"] == "cancelled" for step in result["steps"])
        if row == 884:
            assert result["action_proposal"]["arguments"] == {"zone": "rear_left", "position_percent": 100}
        if row == 885:
            assert result["vehicle_state"]["window_positions"]["rear_left"] == 100
        if row == 886:
            assert len(result["steps"]) == 2
            assert all(step["status"] == "completed" for step in result["steps"])
            assert result["vehicle_state"]["temperature_celsius"] == 25
        if row == 887:
            assert all(step["status"] == "completed" for step in result["steps"])
            assert result["vehicle_state"]["temperature_celsius"] == 27
            assert result["vehicle_state"]["media_playing"]
        if row in {889, 890}:
            assert result["status"] == "unsupported" and "quạt" in result["response_text"], row
        if row in {892, 893, 894}:
            assert result["vehicle_state"]["temperature_celsius"] == 30, (
                row,
                result["output"],
            )
        if row == 895:
            assert "Casual personal" not in result["response_text"]
            assert (
                next(item["value"] for item in services.memory.list("default") if item["key"] == "address_style")
                == "may tao"
            )
        assert all(m["key"] != "display_name" or m["value"] != "tôi" for m in services.memory.list("default")), row
    graph.close()
