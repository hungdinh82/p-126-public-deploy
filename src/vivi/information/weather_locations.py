"""Resolve a weather locality from provider metadata, not a POI whitelist."""

import re

from src.vivi.text import normalize_text


def geocoding_query(location):
    return re.sub(r"^(?:quần đảo|đảo|tỉnh|thành phố|tp\.?|huyện|thị xã)\s+", "", location.strip(), flags=re.I)


def weather_places(places, location):
    valid = [p for p in places if p.get("latitude") is not None and p.get("longitude") is not None]
    local = [p for p in valid if p.get("country_code") == "VN"]
    valid = local or valid
    unique = {}
    for p in valid:
        key = (normalize_text(p.get("name", "")), p.get("country_code"),
               round(p["latitude"], 5), round(p["longitude"], 5))
        unique.setdefault(key, p)
    valid = list(unique.values())
    name = normalize_text(geocoding_query(location).split(",")[0])
    exact = [p for p in valid if normalize_text(p.get("name", "")) == name]
    if exact:
        valid = exact
    if re.match(r"^(?:dao|quan dao)\b", normalize_text(location)):
        islands = [p for p in valid if p.get("feature_code") in {"ISL", "ISLS"}]
        if islands:
            valid = islands
    # Provider-tagged administrative centres own an unqualified locality.
    # PPLA2 resolves a district centre rather than listing its island/village
    # as three identical labels (the logged Phú Quốc response).
    for code in ("PPLC", "PPLA", "PPLA2", "PPLA3", "PPLA4"):
        cities = [p for p in valid if p.get("feature_code") == code]
        if cities:
            return cities
    return valid


def weather_labels(places):
    labels = [", ".join(dict.fromkeys(str(p[k]) for k in ("name", "admin2", "admin1", "country") if p.get(k)))
              for p in places]
    for i, label in enumerate(labels[:]):
        if labels.count(label) > 1:
            kind = "đảo" if places[i].get("feature_code") in {"ISL", "ISLS"} else f"điểm {i + 1}"
            labels[i] = f"{label} ({kind})"
    return labels
