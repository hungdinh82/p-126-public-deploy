"""Shared component fixtures extracted from historical scenario modules."""

from src.vivi.navigation.vietmap import Place, Route, VietMapError
from src.vivi.text import normalize_text
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import station

ORIGIN, DEST, STOP, OPPOSITE = (21.0285, 105.8522), (19.807, 105.776), (20.5, 105.85), (20.4, 105.85)

STOP_NAME = "Trạm Sạc VinFast Trạm Dừng Nghỉ TEST Cao Tốc TEST"


def road(a=ORIGIN, b=DEST, length=180000):
    return Route(
        distance_m=length,
        duration_s=length / 20,
        coordinates=[[a[1], a[0]], [b[1], b[0]]],
        steps=[
            {
                "text": "Đi thẳng",
                "street": "Cao tốc TEST",
                "distance_m": length,
                "duration_s": length / 20,
                "sign": 0,
                "point_index": 0,
            }
        ],
    )


class Maps:
    def __init__(self, *, fail=False, detail=False, opposite=False, missing_leg=False, incompatible=False):
        self.fail, self.detail, self.opposite, self.missing_leg = fail, detail, opposite, missing_leg
        self.queries, self.details, self.roads = [], [], []

    async def search(self, text, **kwargs):
        self.queries.append((text, kwargs))
        if normalize_text(text) in {"thanh hoa", "diem den test thanh hoa"}:
            return [
                Place(
                    ref_id="dest",
                    name="Điểm đến TEST Thanh Hóa",
                    address="Phường TEST, Tỉnh Thanh Hóa",
                    lat=DEST[0],
                    lng=DEST[1],
                )
            ]
        if self.fail:
            raise RuntimeError("map offline")
        places = [
            Place(
                ref_id="s1",
                name=STOP_NAME,
                address="Xã TEST, Tỉnh Ninh Bình",
                lat=None if self.detail else STOP[0],
                lng=None if self.detail else STOP[1],
            ),
            Place(ref_id="wrong-type", name="Trạm Dừng Nghỉ TEST", lat=20.6, lng=105.85),
            Place(ref_id="bike", name="Trạm Sạc Xe Máy VinFast TEST", lat=20.6, lng=105.85),
            Place(ref_id="outside", name="Trạm Sạc VinFast Xa Tuyến TEST", lat=20.5, lng=107),
        ]
        if self.opposite:
            places.append(
                Place(
                    ref_id="s2",
                    name="Trạm Sạc VinFast Trạm Dừng Nghỉ Ngược Chiều TEST",
                    lat=OPPOSITE[0],
                    lng=OPPOSITE[1],
                )
            )
        return places

    async def place(self, ref):
        self.details.append(ref)
        return Place(ref_id=ref, name="", lat=STOP[0], lng=STOP[1])

    async def route(self, a, b):
        self.roads.append((a, b))
        if self.missing_leg and (a, b) == (STOP, DEST):
            raise VietMapError("no access road")
        lengths = {
            (ORIGIN, DEST): 180000,
            (ORIGIN, STOP): 70000,
            (STOP, DEST): 110000,
            (ORIGIN, OPPOSITE): 100000,
            (OPPOSITE, DEST): 150000,
        }
        return road(a, b, lengths[(a, b)])


def equipment(**values):
    return station(lat=STOP[0], lng=STOP[1]).model_copy(
        update={
            "name": STOP_NAME,
            "map_ref_id": "s1",
            "simulated": False,
            "source": "Verified equipment TEST FIXTURE ONLY",
            **values,
        }
    )
