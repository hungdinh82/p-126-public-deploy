"""Shared component fixtures extracted from historical scenario modules."""

from src.vivi.navigation.energy import ChargeBand, ChargingStation, EnergyProfile
from src.vivi.navigation.vietmap import Place, Route, VietMapError
from tests.support.long_term_memory import services as services

HANOI = (21.0287, 105.8524)

HAIPHONG = (20.8449, 106.6881)

STOP = (21.0, 106.2)


class IntercityMaps:
    """Synthetic road lengths; coordinates identify endpoints, not real stations."""

    async def search(self, text, **kwargs):
        return [
            Place(
                ref_id="haiphong-test",
                name="Hải Phòng TEST",
                address="Điểm đại diện mô phỏng",
                lat=HAIPHONG[0],
                lng=HAIPHONG[1],
            )
        ]

    async def route(self, a, b):
        distances = {(HANOI, HAIPHONG): 120000, (HANOI, STOP): 50000, (STOP, HAIPHONG): 70000}
        distance = distances.get((a, b))
        if distance is None:
            raise VietMapError("No verified road fixture")
        return Route(distance_m=distance, duration_s=distance / 20, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


async def progress(graph, nav, identifier, **values):
    return await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": identifier,
            "input_text": "Cập nhật mô phỏng",
            "navigation_event": {"id": identifier, "revision": nav["revision"], **values},
        }
    )


def profile():
    return EnergyProfile(
        id="fixture-ev",
        vehicle_model="VF8",
        model_year=2026,
        edition="TEST ONLY",
        usable_kwh=60,
        consumption_kwh_per_100km=20,
        max_charge_kw=100,
        conservative_factor=1,
        charge_curve=[ChargeBand(until_soc=80, power_kw=100), ChargeBand(until_soc=100, power_kw=40)],
        source="Synthetic hand-calculated test fixture",
        updated_at="2026-10-09",
        simulated=True,
    )


def station(identifier="s1", lat=21, lng=106, power=100):
    return ChargingStation(
        id=identifier,
        name="Trạm sạc VinFast TEST " + identifier,
        lat=lat,
        lng=lng,
        network="vgreen",
        connector="CCS2",
        power_kw=power,
        source="Synthetic test fixture",
        updated_at="2026-10-09",
        simulated=True,
    )


class Maps:
    def __init__(self):
        self.queries = []

    async def search(self, text, near=None, **kwargs):
        self.queries.append((text, near))
        return [
            Place(ref_id="bad", name="Nhất Tâm Logistics"),
            Place(ref_id="a", name="Nhà thuốc TEST", address="TEST Hanoi", lat=21, lng=105.9),
        ]

    async def route(self, a, b):
        distance = abs(b[1] - a[1]) * 1000000
        return Route(
            distance_m=distance,
            duration_s=distance / 20,
            coordinates=[[a[1], a[0]], [b[1], b[0]]],
            steps=[],
        )

    async def place(self, ref):
        return Place(ref_id=ref, name="Nhà thuốc TEST", lat=21, lng=105.9)

    async def matrix(self, points):
        raise RuntimeError("test route fallback")
