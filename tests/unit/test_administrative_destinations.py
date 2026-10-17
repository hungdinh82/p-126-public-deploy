"""Regression for Nam Định and ambiguous names in VietMap v4 boundaries."""

from dataclasses import replace

import pytest

from src.vivi.navigation.destinations import administrative_layer
from src.vivi.navigation.vietmap import Place
from src.vivi.navigation.workflow import NavigationWorkflow
from tests.support.long_term_memory import services as services
from tests.support.navigation_refinement import DestinationMaps


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
