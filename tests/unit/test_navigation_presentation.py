"""Spoken-response regressions from logs 962–965; POI/energy data stay intact."""

import pytest

from src.vivi.navigation.presentation import choice_message, locality, place_label, result_messages, short_name
from src.vivi.navigation.vietmap import Place, Route
from tests.support.long_term_memory import services as services


class LoggedCoffeeMaps:
    def __init__(self):
        self.searches = 0

    async def search(self, text, **kwargs):
        self.searches += 1
        return [
            Place(
                ref_id="habakuk",
                name="Quán Cà Phê Habakuk Ngõ Phan Huy Chú",
                address="Phường Cửa Nam, Thành phố Hà Nội",
                lat=21,
                lng=105.86,
            ),
            Place(
                ref_id="nguoi-mot-nha",
                name="Quán Cà Phê Người Một Nhà Ngõ 183 Đặng Tiến Đông",
                address="Phường Đống Đa, Thành phố Hà Nội",
                lat=21,
                lng=105.87,
            ),
            Place(
                ref_id="hai-tam-hon",
                name="Quán Cà Phê Hai Tâm Hồn Phố Mai Anh Tuấn",
                address="Phường Ô Chợ Dừa, Thành phố Hà Nội",
                lat=21,
                lng=105.88,
            ),
        ]

    async def route(self, a, b):
        return Route(distance_m=3200, duration_s=360, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


@pytest.mark.parametrize(
    "address,expected",
    [
        ("12 Phan Huy Chú, Phường Cửa Nam, TP. Hà Nội", "Phường Cửa Nam"),
        ("Xã An Khánh, Thành phố Hà Nội", "Xã An Khánh"),
        ("Thị trấn TEST, Tỉnh TEST", "Thị trấn TEST"),
        ("TP Hà Nội", ""),
        ("Điểm đại diện mô phỏng", ""),
        ("", ""),
    ],
)
def test_read_locality_without_city(address, expected):
    assert locality(address) == expected


def test_short_names_preserve_brand_and_distinguish_branches():
    assert short_name("Quán Cà Phê Phố Cổ") == "Quán Cà Phê Phố Cổ"
    assert short_name("Quán Cà Phê Phố Cổ Ngõ TEST") == "Quán Cà Phê Phố Cổ"
    assert (
        place_label({"name": "Nhà Thuốc Phường Cửa Nam", "address": "Phường Cửa Nam, TP Hà Nội"})
        == "Nhà Thuốc Phường Cửa Nam"
    )
    message = choice_message(
        [
            {"name": "Nhà Thuốc An Ngõ Một", "address": "Phường TEST, Thành phố Hà Nội"},
            {"name": "Nhà Thuốc An Ngõ Hai", "address": "Phường TEST, Thành phố Hà Nội"},
        ]
    )
    assert "Ngõ Một" in message and "Ngõ Hai" in message
    assert "Thành phố" not in message


def choices(handles, message):
    return {
        "message": message,
        "ui_events": [{"type": "navigation.results"}],
        "navigation_update": {"candidates": [{"handle": handle} for handle in handles]},
    }


def test_duplicate_choice_lists_use_identity_and_keep_other_step_results():
    result = result_messages(
        [
            choices(["a", "b"], "Tìm thấy A và B"),
            {"message": "Đã chỉnh 24 độ"},
            choices(["a", "b"], "A, B. Bạn chọn số mấy?"),
        ]
    )
    assert result == ["Đã chỉnh 24 độ", "A, B. Bạn chọn số mấy?"]
    # A new search with new handles and genuinely repeated effects are retained.
    assert result_messages([choices(["a"], "A"), choices(["new-a"], "A")]) == ["A", "A"]
    assert result_messages([{"message": "Đã bật nhạc"}, {"message": "Đã bật nhạc"}]) == ["Đã bật nhạc"] * 2
