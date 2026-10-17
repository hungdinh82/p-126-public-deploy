"""Identity evidence from map metadata; never interprets user intent."""

import re

from src.vivi.navigation.destinations import region_for
from src.vivi.text import normalize_text


def place_query_matches(query, place):
    """POI evidence check; a nearby unrelated result is not a successful match."""
    parts = [part.strip() for part in query.split(",") if part.strip()]
    query = re.sub(r"\b(?:thanh pho|tp|tinh|phuong|xa)\s+", "", normalize_text(query))
    wanted = set(re.findall(r"[a-z0-9]+", query))
    name = set(re.findall(r"[a-z0-9]+", normalize_text(place.get("name", ""))))
    identity = (
        re.split(r"\b(?:cong|loi vao|chi nhanh|co so|tang|toa|gate|building|tower)\b", normalize_text(parts[0]))[0]
        if parts
        else ""
    )
    identity = re.sub(
        r"^(?:truong(?: (?:tieu hoc|trung hoc(?: co so| pho thong)?|dai hoc|mam non))?|ho|den|lang|ga|cong vien|quang truong|duong|pho|nha hang|khach san|quan (?:ca phe|cafe|pho))\s*\b",
        "",
        identity,
    ).strip()
    identity_words = set(re.findall(r"[a-z0-9]+", identity))
    if (
        identity_words
        and not re.match(r"^\d", identity.strip())
        and len(identity_words & name) / len(identity_words) < 0.75
    ):
        # Shared area words must not make a different venue look like a match.
        return False
    evidence = normalize_text(
        " ".join(
            [
                place.get("name", ""),
                place.get("address", ""),
                *(b.get("full_name") or b.get("name", "") for b in place.get("boundaries", [])),
            ]
        )
    )
    available = set(re.findall(r"[a-z0-9]+", evidence))
    for parent in parts[1:]:
        region = region_for(parent)
        if region and not any(
            re.search(rf"\b{re.escape(normalize_text(name))}\b", evidence)
            for name in [region["name"], *region.get("aliases", [])]
        ):
            return False
    return bool(wanted and wanted.intersection(name) and len(wanted & available) / len(wanted) >= 0.75)


def poi_matches(name, category):
    """Fallback POI identity grammar when no verified vendor category is set."""
    value = normalize_text(name)
    if category == "charging":
        return charging_poi_info(name) is not None
    patterns = {
        "pharmacy": r"\b(?:nha thuoc|hieu thuoc|pharmacy)\b",
    }
    return bool(re.search(patterns[category], value))


def charging_poi_info(name):
    """Identity evidence in a map POI, not a claim about its charger power."""
    value = normalize_text(name)
    if re.search(r"\b(?:xe may|xe dap|doi pin|e-scooter|scooter)\b", value):
        return None
    if not re.search(r"\b(?:tram sac|charging|sac dien)\b", value):
        return None
    if not re.search(r"\b(?:vinfast|v-green|vgreen)\b", value):
        return None
    return {"network": "vgreen", "rest_area": rest_area_poi(name)}


def rest_area_poi(name):
    return bool(re.search(r"\btram dung nghi\b", normalize_text(name)))
