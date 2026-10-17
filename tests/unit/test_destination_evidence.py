"""Replay failures 1354–1379 with synthetic maps and no driving consent."""

import pytest

from src.vivi.capabilities.registry import merge_arguments
from src.vivi.dialogue.contracts import ActionDraft
from src.vivi.navigation.contracts import AreaConstraint, DestinationSpec
from src.vivi.navigation.poi import place_query_matches
from src.vivi.navigation.resolver import PlaceResolver, satisfies_area
from src.vivi.navigation.vietmap import Place, Route
from tests.support.long_term_memory import services as services


def test_plain_query_replacement_removes_stale_structured_destination():
    owner = "navigation"
    old = ActionDraft(
        capability=f"{owner}.refine",
        arguments={
            "query": "Chị, Nam Định",
            "destination": DestinationSpec(name="Chị", area=AreaConstraint(text="Nam Định")),
        },
    )
    incoming = ActionDraft(capability=f"{owner}.refine", arguments={"query": "Hồ Vị Xuyên, Nam Định"})
    merged = merge_arguments(old, incoming)
    assert merged["query"] == "Hồ Vị Xuyên, Nam Định" and "destination" not in merged
    assert ActionDraft(capability=f"{owner}.refine", arguments=merged)


def test_provider_boundary_short_name_matches_a_renamed_parent_area():
    place = {
        "name": "Hồ Vị Xuyên",
        "address": "Phường Nam Định, Tỉnh Ninh Bình",
        "boundaries": [
            {"name": "Nam Định", "full_name": "Phường Nam Định"},
            {"name": "Ninh Bình", "full_name": "Tỉnh Ninh Bình"},
        ],
    }
    assert satisfies_area(place, AreaConstraint(text="Nam Định"))
    assert not satisfies_area(place, AreaConstraint(text="Hà Nội"))
    assert place_query_matches("Hồ Vị Xuyên, Nam Định", place)
    assert not place_query_matches("Hồ Vị Xuyên, Nam Định", {**place, "name": "Hồ Vị Hoàng"})


@pytest.mark.parametrize(
    "query,name,address",
    [
        ("Hồ Vị Xuyên, Nam Định", "Vị Xuyên", "Hồ Vị Xuyên, Phường Nam Định, Tỉnh Ninh Bình"),
        ("Quán phở, Hà Nội", "Phở Gia Truyền", "Quận Hoàn Kiếm, Hà Nội"),
    ],
)
def test_type_descriptors_and_category_queries_are_not_distinctive_venue_names(query, name, address):
    assert place_query_matches(query, {"name": name, "address": address})


@pytest.mark.parametrize(
    "address,expected_calls",
    [
        ("Phường Đông, Nam Định", ["Hồ Nguyệt, Nam Định"]),
        ("Phường Đông, Hà Nội", ["Hồ Nguyệt, Nam Định", "Hồ Nguyệt"]),
    ],
)
@pytest.mark.asyncio
async def test_context_is_a_search_preference_and_can_fall_back_without_wrong_area(address, expected_calls):
    class Search:
        calls = []

        async def search(self, query, **kwargs):
            self.calls.append(query)
            return [Place(ref_id="moon", name="Hồ Nguyệt", address=address, lat=21, lng=105)]

    client = Search()
    spec = DestinationSpec(
        name="Hồ Nguyệt", area=AreaConstraint(text="Nam Định", source="context", constraint="preferred")
    )
    result = await PlaceResolver(client).resolve(spec, {"lat": 21, "lng": 105})
    assert client.calls == expected_calls and result.candidates[0]["address"] == address


class Maps:
    def __init__(self):
        self.queries = []

    async def search(self, query, **kwargs):
        self.queries.append(query)
        if query == "Hồ Hoàn Kiếm, Hà Nội":
            return [Place(ref_id="lake", name="Hồ Hoàn Kiếm", address="Phường Hoàn Kiếm, Hà Nội", lat=21.01, lng=105.9)]
        return []

    async def route(self, a, b):
        return Route(distance_m=2000, duration_s=300, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])
