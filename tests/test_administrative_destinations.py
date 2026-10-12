"""Regression for Nam Định and ambiguous names in VietMap v4 boundaries."""

from dataclasses import replace

import pytest

from src.vivi.dialogue.contracts import NavigationState, SemanticTurn, SessionState
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.navigation.destinations import administrative_layer
from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import Place
from src.vivi.navigation.workflow import NavigationWorkflow
from tests.test_dialogue_v2 import ScriptedModel, build, invoke
from tests.test_long_term_memory import services as services
from tests.test_navigation_refinement import DestinationMaps
from tests.test_navigation_workflow import profile


def admin_place(name="Nam Định", prefix="Phường", parent="Ninh Bình", **extra):
    return Place(
        ref_id=f"{prefix}:{name}:{parent}",
        name=f"{prefix} {name}",
        address=f"{prefix} {name}, {parent}",
        boundaries=[
            {"type": 2, "name": name, "full_name": f"{prefix} {name}"},
            {"type": 0, "name": parent, "full_name": f"Tỉnh {parent}"},
        ],
        **extra,
    )


class MergedMaps(DestinationMaps):
    def __init__(self, *, ambiguous=False, invalid_ward=False):
        super().__init__()
        self.ambiguous, self.invalid_ward = ambiguous, invalid_ward
        self.details = []

    async def search(self, text, **kwargs):
        self.queries.append((text, kwargs))
        if kwargs.get("layers") == "CITY":
            # Actual v4 behavior: former Nam Định province maps to Ninh Bình.
            return [
                Place(
                    ref_id="merged",
                    name="Tỉnh Ninh Bình",
                    boundaries=[{"type": 0, "name": "Ninh Bình", "full_name": "Tỉnh Ninh Bình"}],
                )
            ]
        assert kwargs.get("layers") == "WARD"
        if self.invalid_ward:
            return [admin_place(categories=["business"]), admin_place(name="Nam Định Tây")]
        if self.ambiguous:
            return [admin_place("Hội An", parent="Đà Nẵng"), admin_place("Hội An", "Xã", "An Giang")]
        return [admin_place()]

    async def place(self, ref):
        self.details.append(ref)
        return Place(ref_id=ref, name="Provider details", lat=20.43, lng=106.17)


@pytest.mark.parametrize("text", ["Chỉ đường về phường Nam Định.", "phường Nam Định"])
def test_model_must_preserve_explicit_ward_in_pending_refinement(text):
    session = SessionState(
        navigation=NavigationState(
            status="selecting",
            revision=2,
            clarification={"region": "Nam Định", "phase": "specific", "revision": 2, "choices": []},
        )
    )
    semantic = SemanticTurn.model_validate(
        {"act": "modify", "actions": [{"capability": "navigation.refine", "arguments": {"query": "Nam Định"}}]}
    )
    action = OfflineParser().guard(text, semantic, session).actions[0]
    assert action.capability == "navigation.refine"
    assert action.arguments.query == "phường Nam Định"


@pytest.mark.parametrize(
    "query,layer",
    [
        ("Phường Nam Định, Ninh Bình", "WARD"),
        ("xã Hội An", "WARD"),
        ("tỉnh Ninh Bình", "CITY"),
        ("trung tâm phường Hoa Lư", "WARD"),
        ("Nhà thuốc Phường Nam Định", None),
        ("Nam Định", None),
        ("quán cà phê", None),
        ("quan ca phe", None),
        ("quận 1", "DIST"),
    ],
)
def test_only_explicit_administrative_prefix_sets_layer(query, layer):
    assert administrative_layer(query) == layer


@pytest.mark.asyncio
async def test_merged_region_retries_ward_after_mismatching_city_before_hydration(services):
    maps = MergedMaps()
    candidates = await NavigationWorkflow(replace(services, navigation=maps)).search(
        {"query": "Nam Định"},
        {"lat": 21, "lng": 105.85},
        representative=True,
    )
    assert [args["layers"] for _, args in maps.queries] == ["CITY", "WARD"]
    assert maps.details == ["Phường:Nam Định:Ninh Bình"]
    assert len(candidates) == 1 and candidates[0]["name"] == "Phường Nam Định"
    assert candidates[0]["lat"] == 20.43 and candidates[0]["boundaries"]


@pytest.mark.asyncio
async def test_fallback_rejects_business_and_partial_name(services):
    maps = MergedMaps(invalid_ward=True)
    candidates = await NavigationWorkflow(replace(services, navigation=maps)).search(
        {"query": "Nam Định"},
        {"lat": 21, "lng": 105.85},
        representative=True,
    )
    assert candidates == [] and not maps.details


@pytest.mark.asyncio
async def test_explicit_ward_checks_parent_and_does_not_retry_different_layer(services):
    maps = MergedMaps()
    candidates = await NavigationWorkflow(replace(services, navigation=maps)).search(
        {"query": "Phường Nam Định, Hải Phòng"},
        {"lat": 21, "lng": 105.85},
    )
    assert candidates == [] and not maps.details
    assert len(maps.queries) == 1 and maps.queries[0][1]["layers"] == "WARD"


@pytest.mark.asyncio
async def test_explicit_ward_center_and_parent_resolve_without_alias_catalogue(services):
    maps = MergedMaps()
    candidates = await NavigationWorkflow(replace(services, navigation=maps)).search(
        {"query": "trung tâm phường Nam Định, Ninh Bình"},
        {"lat": 21, "lng": 105.85},
    )
    assert len(candidates) == 1 and candidates[0]["name"] == "Phường Nam Định"
    assert len(maps.queries) == 1


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_nam_dinh_region_then_explicit_ward_produces_consent_preview(services, tmp_path, tier):
    maps = MergedMaps()
    runtime = replace(services, navigation=maps, energy_planner=EnergyPlanner(profile()))
    model = ScriptedModel(
        [
            {"act": "request", "actions": [{"capability": "navigation.search", "arguments": {"query": "Nam Định"}}]},
            {"act": "modify", "actions": [{"capability": "navigation.refine", "arguments": {"query": "Nam Định"}}]},
        ]
    )
    graph, _, gateway = build(
        runtime, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        first = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "region",
                "input_text": "Chỉ đường về Nam Định.",
                "position": {"lat": 21, "lng": 105.85, "simulated": True},
            }
        )
        assert first["navigation"]["selected"]["name"] == "Phường Nam Định"
        assert first["navigation"]["clarification"]["phase"] == "change_destination"
        result = await invoke(graph, "Chỉ đường về phường Nam Định.", "ward")
        assert maps.queries[-1][0] == "phường Nam Định"  # no obsolete parent appended
        assert maps.queries[-1][1]["layers"] == "WARD"
        assert result["navigation"]["selected"]["name"] == "Phường Nam Định"
        assert result["navigation"]["status"] == "preview"
        assert result["navigation"]["offer"]["status"] == "pending"
        assert not result["navigation"]["clarification"]
        assert not (await gateway.vehicle.get_state("driver")).driving
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_same_name_wards_in_different_provinces_require_selection(services, tmp_path):
    maps = MergedMaps(ambiguous=True)
    graph, _, _ = build(replace(services, navigation=maps, energy_planner=EnergyPlanner(profile())), tmp_path)
    try:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "hoi-an",
                "input_text": "Chỉ đường tới Hội An.",
                "position": {"lat": 21, "lng": 105.85, "simulated": True},
            }
        )
        assert len(result["navigation"]["candidates"]) == 2
        assert result["navigation"]["status"] == "selecting"
        assert result["navigation"]["selected"] is None
        assert "Đà Nẵng" in result["response_text"] and "An Giang" in result["response_text"]
    finally:
        graph.close()
