"""Destination evidence and configurable aliases, independent of graph routing."""

import json
import re
from functools import lru_cache
from pathlib import Path

from src.vivi.text import normalize_text


@lru_cache(maxsize=1)
def regions():
    path = Path(__file__).resolve().parents[3] / "data/navigation/regions.json"
    return json.loads(path.read_text(encoding="utf-8"))["regions"]


@lru_cache(maxsize=1)
def landmarks():
    path = Path(__file__).resolve().parents[3] / "data/navigation/landmarks.json"
    return json.loads(path.read_text(encoding="utf-8"))["landmarks"]


def landmark_for(query):
    parts = [part.strip() for part in (query or "").split(",")]
    for item in landmarks():
        if normalize_text(parts[0]) not in {normalize_text(name) for name in [item["name"], *item["aliases"]]}:
            continue
        if len(parts) == 1 or len(parts) == 2 and region_for(parts[1]) == region_for(item["region"]):
            return item
    return None


def landmark_query(query):
    item = landmark_for(query)
    return f"{item['name']}, {item['region']}" if item else query


def qualify_place_query(query, area):
    parts = [part.strip() for part in query.split(",") if part.strip()]
    if parts and region_for(parts[-1]):
        parts.pop()
    return ", ".join([*parts, area])


def center_requested(query):
    return bool(re.match(r"^trung tam\s+", normalize_text(query or "").strip()))


def region_name(query):
    value = re.sub(r"[.,]+", " ", normalize_text(query or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"^trung tam\s+(?:cua\s+)?", "", value)


def region_for(query):
    value = region_name(query)
    for region in regions():
        aliases = [region["name"], *region.get("aliases", [])]
        if any(
            value
            in {
                region_name(name),
                "tinh " + region_name(name),
                "thanh pho " + region_name(name),
                "tp " + region_name(name),
            }
            for name in aliases
        ):
            return region
    return None


def administrative_layer(query):
    """Only an explicit administrative prefix constrains the search layer."""
    value = region_name((query or "").split(",")[0])
    for prefix, layer in (
        (r"(?:phuong|xa|thi tran)", "WARD"),
        (r"(?:huyen|thi xa)", "DIST"),
        (r"(?:tinh|thanh pho|tp)", "CITY"),
    ):
        if re.match(rf"^{prefix}\s+\S", value):
            return layer
    # Accent folding makes "quán cà phê" and "quận" both start with "quan".
    # A district constraint needs the explicit, accented administrative prefix.
    if re.match(r"^quận\s+\S", (query or "").strip(), re.I):
        return "DIST"
    return None


def accented_name_match(place, query):
    """Same administrative name with the accents the user actually said."""
    import unicodedata

    def bare(value):
        value = unicodedata.normalize("NFC", value or "").casefold().split(",")[0].strip(" .!?")
        return re.sub(r"^(?:phường|xã|thị trấn|đặc khu|thành phố|tỉnh)\s+", "", value)

    return bare(place.get("name", "")) == bare(query)


def administrative_match(place, query):
    """An address inside a region does not make its business an admin result."""
    value = normalize_text(place.get("name", ""))
    parts = [normalize_text(part).strip(" .!?") for part in query.split(",") if part.strip()]
    if not parts:
        return False
    expected = re.sub(r"^trung tam\s+(?:cua\s+)?", "", parts[0])
    boundaries = place.get("boundaries", [])
    boundary_names = {normalize_text(b.get(key, "")) for b in boundaries for key in ("full_name", "name")}
    parents = [region_for(part) for part in parts[1:]]
    return (
        not place.get("categories")
        and all(
            normalize_text(region["name"]) in boundary_names if region else part in boundary_names
            for part, region in zip(parts[1:], parents)
        )
        and any(
            expected in {normalize_text(b.get("full_name", "")), normalize_text(b.get("name", ""))}
            and value in {expected, normalize_text(b.get("full_name", "")), normalize_text(b.get("name", ""))}
            for b in boundaries
        )
    )
