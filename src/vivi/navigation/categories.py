"""Nearby place categories: VietMap codes plus folded name and utterance cues.

Codes were read from VietMap autocomplete v4 results (``categories``). A code
narrows the provider search; identity is still checked on the returned POI so
a café named "Cây Xăng" is never offered as a fuel station.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.vivi.text import normalize_text


@dataclass(frozen=True)
class PoiCategory:
    key: str
    label: str
    query: str
    codes: tuple[str, ...]
    names: str
    terms: str
    exclude: str | None = None


# Order matters for utterance cues: specific kinds come before generic ones
# ("nhà thuốc bệnh viện" is a pharmacy, "quán cà phê" is not a restaurant).
CATEGORIES = {
    item.key: item
    for item in (
        PoiCategory(
            "charging",
            "trạm sạc",
            "trạm sạc ô tô",
            ("10013-1",),
            r"\b(?:tram sac|tru sac|charging|sac dien|v-?green)\b",
            r"\b(?:tram sac|tru sac|cho sac|sac xe|sac dien|sac pin|charging)\b",
            r"\b(?:xe may|xe dap|doi pin|e-scooter|scooter)\b",
        ),
        PoiCategory(
            "pharmacy",
            "nhà thuốc",
            "nhà thuốc",
            ("3004-39",),
            r"\b(?:nha thuoc|hieu thuoc|quay thuoc|pharmacy|pharmacity|long chau)\b",
            r"\b(?:nha thuoc|hieu thuoc|quay thuoc|mua thuoc)\b",
            r"\bkinh\b",
        ),
        PoiCategory(
            "hospital",
            "bệnh viện",
            "bệnh viện",
            ("7003",),
            r"\b(?:benh vien|hospital|vinmec)\b",
            r"\b(?:benh vien|cap cuu)\b",
            r"\b(?:nha thuoc|kinh thuoc|tap the|nha an)\b",
        ),
        PoiCategory(
            "clinic",
            "phòng khám",
            "phòng khám",
            ("7002",),
            r"\b(?:phong kham|clinic|tram y te)\b",
            r"\b(?:phong kham|tram y te)\b",
        ),
        PoiCategory(
            "cafe",
            "quán cà phê",
            "cà phê",
            ("1001",),
            r"\b(?:ca phe|cafe|caphe|coffee|kaffe|highlands|starbucks|cong caphe|phuc long|tra sua)\b",
            r"\b(?:ca phe|cafe|caphe|coffee|highlands|starbucks|phuc long|uong nuoc)\b",
        ),
        PoiCategory(
            "gas_station",
            "cây xăng",
            "cây xăng",
            ("10009",),
            r"\b(?:cay xang|tram xang|xang dau|petrolimex|pvoil)\b",
            r"\b(?:cay xang|tram xang|do xang|xang dau)\b",
            r"\b(?:quan|kaffe|cafe|buffet|lau)\b",
        ),
        PoiCategory(
            "restaurant",
            "nhà hàng",
            "nhà hàng",
            ("1002",),
            r"\b(?:nha hang|quan an|restaurant|buffet|com|pho|bun|lau)\b",
            r"\b(?:nha hang|quan an|an trua|an toi|an sang|cho an)\b",
        ),
        PoiCategory(
            "parking",
            "bãi đỗ xe",
            "bãi đỗ xe",
            ("10005",),
            r"\b(?:bai do|bai gui|do xe|gui xe|parking)\b",
            r"\b(?:bai do|bai gui|cho do|do xe|gui xe|parking)\b",
        ),
        PoiCategory(
            "supermarket",
            "siêu thị",
            "siêu thị",
            ("3002",),
            r"\b(?:sieu thi|winmart|coopmart|mart|supermarket)\b",
            r"\b(?:sieu thi|cua hang tien loi)\b",
        ),
        PoiCategory(
            "hotel",
            "khách sạn",
            "khách sạn",
            ("2001",),
            r"\b(?:khach san|hotel|nha nghi|homestay)\b",
            r"\b(?:khach san|nha nghi|cho ngu)\b",
        ),
        PoiCategory(
            "atm",
            "cây ATM",
            "atm",
            ("6008",),
            r"\batm\b",
            r"\b(?:atm|rut tien)\b",
        ),
        PoiCategory(
            "toilet",
            "nhà vệ sinh",
            "nhà vệ sinh công cộng",
            ("8003-3",),
            r"\b(?:nha ve sinh|wc|toilet)\b",
            r"\b(?:nha ve sinh|wc|toilet|di ve sinh)\b",
        ),
    )
}
CATEGORY_KEYS = tuple(CATEGORIES)

NEAREST_CUES = r"\b(?:gan nhat|gan day nhat|gan day|gan toi|gan minh|gan xe|quanh day|xung quanh|lan can)\b"
# "gần vị trí hiện tại của tôi nhất" splits the superlative around its anchor.
SPLIT_NEAREST = r"\bgan (?:\w+ ){1,6}nhat\b"
# Words that remain once a generic category request lost its category term.
FILLER = r"\b(?:tim|cho|toi|minh|mot|cai|nao|nhat|nhe|di|den|toi|duong|chi|quan|tiem|o|co|khong|vi tri|hien tai|cua)\b"


def category_for_text(text):
    """The category an utterance or query names, by its first cue."""
    value = normalize_text(text or "")
    found = [
        (match.start(), index, key)
        for index, (key, item) in enumerate(CATEGORIES.items())
        for match in [re.search(item.terms, value)]
        if match
    ]
    return min(found)[2] if found else None


def asks_nearest(text):
    value = normalize_text(text or "")
    return bool(re.search(NEAREST_CUES, value) or re.search(SPLIT_NEAREST, value))


def strip_nearest(text):
    """Remove spoken proximity cues; keep the original accents of the rest."""
    words = (text or "").split()
    kept = []
    skip = 0
    folded = [normalize_text(word) for word in words]
    for index in range(len(words)):
        if skip:
            skip -= 1
            continue
        for size in (3, 2):
            phrase = " ".join(folded[index : index + size])
            if len(folded[index : index + size]) == size and re.fullmatch(NEAREST_CUES, phrase):
                skip = size - 1
                break
        else:
            kept.append(words[index])
    return " ".join(kept).strip(" ,?.!") or (text or "").strip()


def is_generic(text, key):
    """True when the query names only the category ("bệnh viện gần nhất")."""
    item = CATEGORIES.get(key)
    if item is None:
        return False
    value = re.sub(NEAREST_CUES, " ", normalize_text(text or ""))
    value = re.sub(item.terms, " ", value)
    value = re.sub(FILLER, " ", value)
    return not value.split()


def reconcile_category(query, proposed=None, *, nearest=False):
    """Host-side category: the spoken query wins over a model's guess.

    A named place ("Bệnh viện Bạch Mai") stays a named search unless the model
    or a proximity cue asked for a category.
    """
    inferred = category_for_text(query)
    if inferred and (proposed or nearest or is_generic(query, inferred)):
        return inferred
    return proposed if proposed in CATEGORIES and not inferred else None


def matches_category(place, key):
    """Provider code first; a name cue covers POIs without a precise code."""
    item = CATEGORIES.get(key)
    if item is None:
        return False
    name = normalize_text(place.get("name", ""))
    if item.exclude and re.search(item.exclude, name):
        return False
    codes = {str(code) for code in place.get("categories") or []}
    if codes & set(item.codes):
        return True
    families = {code.split("-")[0] for code in item.codes}
    if codes and not {code.split("-")[0] for code in codes} & families:
        return False
    return bool(re.search(item.names, name))
