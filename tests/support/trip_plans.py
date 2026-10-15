"""Shared component fixtures extracted from historical scenario modules."""

from dataclasses import replace

from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.vietmap import Place, Route
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import profile, station

ORIGIN = (21, 105.8)

DEST = (20, 105.8)

CHARGER = (20.5, 105.8)


class TripMaps:
    def __init__(self):
        self.queries = []
        self.routes = []

    async def search(self, query, **kwargs):
        self.queries.append((query, kwargs))
        if kwargs.get("layers"):
            name = "Phường Hoa Lư" if "Hoa Lư" in query else "Nghi Sơn" if "Nghi Sơn" in query else query
            return [
                Place(
                    ref_id="center-fixture",
                    name=name,
                    address=query,
                    lat=DEST[0],
                    lng=DEST[1],
                    boundaries=[{"name": name, "full_name": name}, {"name": "Ninh Bình"}, {"name": "Thanh Hoá"}],
                )
            ]
        return [Place(ref_id="place-fixture", name=query, address="Phường Hoa Lư", lat=DEST[0], lng=DEST[1])]

    async def route(self, a, b):
        self.routes.append((a, b))
        distances = {
            (ORIGIN, DEST): 120000,
            (DEST, ORIGIN): 150000,
            (ORIGIN, CHARGER): 60000,
            (CHARGER, DEST): 60000,
            (DEST, CHARGER): 80000,
            (CHARGER, ORIGIN): 70000,
        }
        if a == b:
            distance = 0
        else:
            distance = distances[a, b]
        return Route(distance_m=distance, duration_s=distance / 20, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


def runtime(services, soc=82):
    maps = TripMaps()
    services.action_gateway.vehicle.set_energy("driver", soc, 300)
    return replace(
        services, navigation=maps, energy_planner=EnergyPlanner(profile(), [station(lat=CHARGER[0], lng=CHARGER[1])])
    ), maps
