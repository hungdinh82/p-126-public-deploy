"""Destination evidence and configurable aliases, independent of graph routing."""

import json
from functools import lru_cache
from pathlib import Path

from src.vivi.text import normalize_text


@lru_cache(maxsize=1)
def regions():
    path = Path(__file__).resolve().parents[3] / "data/navigation/regions.json"
    return json.loads(path.read_text(encoding="utf-8"))["regions"]


def region_for(query):
    value = normalize_text(query or "")
    for region in regions():
        aliases = [region["name"], *region.get("aliases", [])]
        if any(
            value
            in {
                normalize_text(name),
                "tinh " + normalize_text(name),
                "thanh pho " + normalize_text(name),
                "tp " + normalize_text(name),
            }
            for name in aliases
        ):
            return region
    return None


def administrative_match(place, query):
    """An address inside a region does not make its business an admin result."""
    value = normalize_text(place.get("name", ""))
    expected = normalize_text(query)
    boundaries = place.get("boundaries", [])
    return not place.get("categories") and any(
        expected in {normalize_text(b.get("full_name", "")), normalize_text(b.get("name", ""))}
        and value in {expected, normalize_text(b.get("full_name", "")), normalize_text(b.get("name", ""))}
        for b in boundaries
    )
