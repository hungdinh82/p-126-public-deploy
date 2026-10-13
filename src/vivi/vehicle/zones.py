from __future__ import annotations

from collections.abc import Mapping

CABIN_ZONES = ("driver", "front_passenger", "rear_left", "rear_right")
ZONE_LABELS = {
    "driver": "bên tài",
    "front_passenger": "bên phụ",
    "rear_left": "sau trái",
    "rear_right": "sau phải",
    "all": "tất cả",
}

CABIN_ZONE_CHOICES = "bên tài, bên phụ, sau trái, sau phải hoặc tất cả"

# Body panels opened as a whole, keyed by intent: (state field, spoken name).
BODY_PANELS = {
    "hood.set_open": ("hood_open", "nắp capo"),
    "trunk.set_open": ("trunk_open", "cốp sau"),
}

# Every door, the hood and the tailgate in one action.
ALL_PANELS_LABEL = "tất cả cửa, nắp capo và cốp sau"


def selected_zones(arguments: Mapping[str, object]) -> tuple[str, ...]:
    if arguments.get("zones") is not None:
        zones = arguments["zones"]
        if arguments.get("zone") is not None or not isinstance(zones, list) or not zones:
            raise ValueError("Chỉ chọn một phạm vi cabin.")
        if len(set(zones)) != len(zones) or any(zone not in CABIN_ZONES for zone in zones):
            raise ValueError("Vị trí cabin không hợp lệ.")
        return tuple(zone for zone in CABIN_ZONES if zone in zones)
    zone = arguments.get("zone", "driver")
    if zone == "all":
        return CABIN_ZONES
    if isinstance(zone, str) and zone in CABIN_ZONES:
        return (zone,)
    raise ValueError("Vị trí cabin không hợp lệ.")


def zone_label(arguments: Mapping[str, object]) -> str:
    if arguments.get("zones") is not None:
        return ", ".join(ZONE_LABELS[zone] for zone in selected_zones(arguments))
    zone = arguments.get("zone", "driver")
    return ZONE_LABELS.get(str(zone), str(zone))


def zoned_noun(noun: str, arguments: Mapping[str, object]) -> str:
    return f"tất cả {noun}" if arguments.get("zone") == "all" or arguments.get("zones") == list(CABIN_ZONES) else f"{noun} {zone_label(arguments)}"
