"""Shared cabin slot parsers; no routing or execution side effects."""

import re


def cabin_zone(text: str) -> str | None:
    if re.search(r"\b(tat ca|toan bo|ca bon|4)\b", text):
        return "all"
    if re.search(r"\b(sau|hang sau|phia sau).{0,12}\b(trai)\b", text):
        return "rear_left"
    if re.search(r"\b(sau|hang sau|phia sau).{0,12}\b(phai)\b", text):
        return "rear_right"
    if re.search(r"\b(ben phu|ghe phu|truoc phai)\b", text):
        return "front_passenger"
    if re.search(r"\b(ben tai|ben lai|tai xe|truoc trai)\b", text):
        return "driver"
    return None


def is_cabin_zone_reply(text: str) -> bool:
    """Accept a location-only clarification reply, not a new sentence mentioning one."""
    zone = (
        r"(?:ben tai|ben lai|tai xe|truoc trai|ben phu|ghe phu|truoc phai|"
        r"(?:sau|hang sau|phia sau)(?:\s+ben)?\s+(?:trai|phai)|"
        r"tat ca|toan bo|ca bon|4)"
    )
    return bool(
        re.fullmatch(
            rf"(?:(?:o|cua|ghe)\s+)?{zone}(?:\s+(?:nhe|a|giup minh|giup toi))?",
            text.strip(),
        )
    )


def temperature_number(text: str) -> float | None:
    numeric = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:do|°)", text)
    if not numeric:
        numeric = re.search(
            r"(?:nhiet do|dieu hoa).{0,20}?(\d+(?:[.,]\d+)?)\b",
            text,
        )
    if numeric:
        return float(numeric.group(1).replace(",", "."))
    words = {
        "khong": 0,
        "mot": 1,
        "hai": 2,
        "ba": 3,
        "bon": 4,
        "tu": 4,
        "nam": 5,
        "sau": 6,
        "bay": 7,
        "tam": 8,
        "chin": 9,
        "muoi": 10,
    }
    match = re.search(
        r"\b(khong|mot|hai|ba|bon|tu|nam|sau|bay|tam|chin|muoi)\s*(?:do|°)",
        text,
    )
    return float(words[match.group(1)]) if match else None
