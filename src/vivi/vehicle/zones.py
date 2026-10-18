from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from src.vivi.text import normalize_text

CABIN_ZONES = ("driver", "front_passenger", "rear_left", "rear_right")
# Vocabulary for a position, not complete user commands or model answers.
CABIN_ZONE_ALIASES = {
    "driver": (
        "ghế lái",
        "ghế tài xế",
        "tài xế",
        "người lái",
        "bên tài",
        "bên lái",
        "cửa lái",
        "cửa tài",
        "trước trái",
        "trái trước",
    ),
    "front_passenger": ("ghế phụ", "bên phụ", "cửa phụ", "phụ", "hành khách", "trước phải", "phải trước"),
    "rear_left": ("sau trái", "trái sau"),
    "rear_right": ("sau phải", "phải sau"),
}


def spoken_zone_aliases(text):
    """Accept missing accents, but preserve explicit different word meanings."""
    spelling = unicodedata.normalize("NFC", text.casefold())
    folded = normalize_text(spelling)
    found = set()
    for zone, aliases in CABIN_ZONE_ALIASES.items():
        for alias in aliases:
            value = normalize_text(alias)
            for match in re.finditer(r"(?<!\w)" + re.escape(value) + r"(?!\w)", folded):
                actual = spelling[match.start() : match.end()]
                if len(actual) == len(alias) and any(
                    a != b and a != normalize_text(a) and b != normalize_text(b)
                    for a, b in zip(actual, alias, strict=True)
                ):
                    continue
                found.add(zone)
    return found


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
    return (
        f"tất cả {noun}"
        if arguments.get("zone") == "all" or arguments.get("zones") == list(CABIN_ZONES)
        else f"{noun} {zone_label(arguments)}"
    )
