"""Shared component fixtures extracted from historical scenario modules."""

from src.vivi.navigation.vietmap import Place, Route
from tests.support.long_term_memory import services as services


class DestinationMaps:
    def __init__(self):
        self.queries = []

    async def search(self, text, **kwargs):
        self.queries.append((text, kwargs))
        if kwargs.get("layers") == "WARD":
            return [
                Place(
                    ref_id="hoa-lu",
                    name="Phường Hoa Lư",
                    address="Tỉnh Ninh Bình",
                    lat=20.25,
                    lng=105.95,
                    boundaries=[
                        {"type": 2, "name": "Hoa Lư", "full_name": "Phường Hoa Lư"},
                        {"type": 0, "name": "Ninh Bình", "full_name": "Tỉnh Ninh Bình"},
                    ],
                )
            ]
        return [
            Place(ref_id="trang-an", name="Tràng An", address="Phường Hoa Lư, Tỉnh Ninh Bình", lat=20.25, lng=105.95)
        ]

    async def route(self, a, b):
        return Route(distance_m=80000, duration_s=4000, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])
