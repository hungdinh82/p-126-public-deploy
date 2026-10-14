"""Source selection depends on the requested fact, not model confidence."""

import re
from dataclasses import dataclass

from src.vivi.text import normalize_text


@dataclass(frozen=True)
class InformationDecision:
    capability: str
    reason: str


def clock_question(query):
    value = normalize_text(query).strip(" .!?")
    return bool(re.fullmatch(
        r"(?:hom nay (?:la )?(?:ngay (?:bao nhieu|may)|thu may)|"
        r"(?:nam nay|bay gio|hien tai) (?:la )?nam (?:bao nhieu|may)|"
        r"(?:bay gio|hien tai) (?:la )?may gio)", value
    ))


def information_decision(query):
    value = normalize_text(query)
    if re.search(r"\b(?:ke (?:cho .+ )?(?:chuyen|truyen)|ke tiep|truyen ngan|chuyen vui)\b", value):
        return None
    if re.match(r"^(?:toi|minh) (?:dang )?(?:la|lam)\b", value) and "?" not in query:
        return None
    if clock_question(query):
        return InformationDecision("conversation.respond", "runtime_clock")
    # Explicit search requests take precedence over stable encyclopedic subjects.
    if re.search(r"\b(?:tim (?:tren )?mang|tra (?:tren )?mang|internet|google|tim kiem tren)\b", value):
        return InformationDecision("web.search", "explicit_search")
    if re.search(r"\b(?:thoi tiet|du bao|troi.*(?:mua|nang)|co mua|mua khong)\b", value):
        return InformationDecision("weather.get_forecast", "forecast_api")
    role = r"(?:tong bi thu|chu tich|thu tuong|tong thong|hieu truong|giam doc|ceo|lanh dao|bo truong)"
    historical = re.search(r"\b(?:lich su|truoc day|dau tien)\b", value)
    definition = re.search(r"\b(?:la gi|vai tro|nhiem vu|quyen han)\b", value)
    if re.search(rf"\b{role}\b", value) and not historical and not definition:
        return InformationDecision("web.search", "current_office_holder")
    if re.search(r"\b(?:bao nhieu|so luong|danh sach)\b.*\b(?:tinh thanh|tinh|thanh pho|dan so)\b|"
                 r"\b(?:dan so|dien tich|sap nhap|dia gioi|don vi hanh chinh)\b|\brong\b.*\bbao nhieu\b", value) and not historical and not re.search(r"\b(?:xe|vf\d+|pin|lop|cua)\b", value):
        return InformationDecision("web.search", "changing_geographic_fact")
    if re.search(r"\b(?:tin moi|moi nhat|gia vang|ty gia|gia xang|gia ban|khuyen mai|gia ve|gio mo cua|"
                 r"lich hoat dong|su kien|du lich|tham quan|gi choi|choi gi|gi vui|cho nao dang di)\b", value):
        return InformationDecision("web.search", "current_or_recommendation")
    if re.search(r"\b(?:gia (?:xe|o to|lan banh|vf\d+|cua)|co gia (?:bao nhieu|the nao)|gia\b.*\bbao nhieu|bao nhieu tien)\b", value):
        return InformationDecision("web.search", "product_price")
    if re.search(r"\b(?:muc phat|bi phat bao nhieu|quy dinh|luat hien hanh|lai suat|thue suat)\b", value):
        return InformationDecision("web.search", "current_regulation_or_rate")
    inquiry = "?" in query or re.search(r"\b(?:ai|bao nhieu|la gi|co gi|cho biet|thong tin|tra cuu)\b", value)
    if inquiry and re.search(r"\b(?:hien nay|hien tai|bay gio|hom nay|nam nay|dang la)\b", value):
        # Vehicle observations are owned by the vehicle route, never this policy.
        if not re.search(r"\b(?:pin|xe|lop|cua|dieu hoa|vf\d+)\b", value):
            return InformationDecision("web.search", "explicit_current_fact")
    if re.search(r"\b(?:wikipedia|lich su|van hoa|nguon goc)\b", value):
        return InformationDecision("knowledge.lookup", "encyclopedic")
    # Keep Vietnamese diacritics: "tìm hiệu thuốc" is a place search, not
    # "tìm hiểu". Referenced vehicle features still belong to the planner/RAG.
    if re.match(r"^(?:giới thiệu|tìm hiểu|thông tin|gioi thieu|thong tin)\s+(?:(?:qua|sơ lược)\s+)?(?:(?:cho (?:tôi|mình))\s+)?(?:về\s+)?\S", query.strip(), re.I) and not re.search(
        r"\b(?:xe|vf\d+|vinfast|pin|sac|lop|phanh|dieu hoa|tinh nang|adas)\b|\b(?:day|do|ay|nay)[.!? ]*$", value
    ):
        return InformationDecision("knowledge.lookup", "subject_overview")
    return None


def encyclopedia_topic(query):
    """Keep the entity, rather than every interrogative term, in Wiki search."""
    text = query.strip(" .!?")
    text = re.sub(r"^(?:giới thiệu|tìm hiểu|thông tin)\s+(?:(?:qua|sơ lược)\s+)?(?:(?:cho (?:tôi|mình))\s+)?(?:về\s+)?", "", text, flags=re.I)
    text = re.sub(r"^(?:tra (?:cứu )?(?:wikipedia|wiki)(?: về)?|lịch sử(?: của)?|văn hóa(?: của)?|nguồn gốc(?: của)?)\s+", "", text, flags=re.I)
    text = re.sub(r"\s+(?:rộng|có diện tích|có dân số|có bao nhiêu|là gì|là ai)\b.*$", "", text, flags=re.I)
    text = re.sub(r"^(?:diện tích|dân số)(?: của)?\s+", "", text, flags=re.I)
    return text.strip(" .!?") or query


def retrieval_query(query):
    """Disambiguate retail vehicle prices without changing the answer question."""
    value = normalize_text(query)
    vehicle_price = re.search(r"\bgia\b|\bbao nhieu tien\b", value) and re.search(r"\b(?:vinfast|vf\s*-?\s*\d+)\b", value)
    explicit_scope = re.search(r"\b(?:cu|da qua su dung|second.hand|used|lan banh|thue|thue mua|my|usd|chau au)\b", value)
    if vehicle_price and not explicit_scope:
        return query.rstrip(" .!?") + " giá niêm yết xe mới tại Việt Nam"
    return query
