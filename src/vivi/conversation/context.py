"""Small, typed-by-owner views of navigation and conversational references."""

import re

from src.vivi.text import normalize_text


def destination(session):
    workspace = session.navigation_workspace
    # A running journey takes precedence over an unaccepted replacement draft;
    # a finished one never shadows the draft being planned now.
    owner = workspace.live_journey or workspace.draft or workspace.visible_journey
    selected = (owner.selected or {}) if owner else {}
    if not selected and session.trip_plan:
        selected = session.trip_plan.selected or {}
    return {key: selected[key] for key in ("name", "address", "lat", "lng", "handle") if key in selected}


def conversation_view(session):
    return {"destination": destination(session), "recent_topic": session.conversation_context}


PLACE_REFERENCE = r"\b(?:(?:ở|tại) (?:đó|đấy|ấy)|(?:nơi|chỗ|thành phố|tỉnh) (?:đó|đấy|ấy))\b"


def topic_subject(query):
    """Extract a bounded subject only from an explicit, simple question frame.

    No geocoder or guessed city: unfamiliar names work too. Unclear subjects
    remain unresolved instead of borrowing an unrelated navigation destination.
    """
    value = query.strip(" .!?")
    value = re.sub(r"^(?:ở|tại)\s+", "", value, flags=re.I)
    value = re.sub(r"^(?:giới thiệu|tìm hiểu|thông tin|lịch sử|văn hóa|nguồn gốc|dân số|diện tích)\s+(?:(?:qua|sơ lược)\s+)?(?:(?:cho (?:tôi|mình))\s+)?(?:(?:về|của|ở|tại)\s+)?", "", value, flags=re.I)
    match = re.match(r"^(.+?)\s+(?:có|rộng|là|được|dân số|diện tích)\b", value, flags=re.I)
    if match:
        value = match[1]
    elif value == query.strip(" .!?"):
        return None
    if not value or len(value) > 100 or re.search(r"[;?()]|\b(?:và|hay|hoặc)\b", value, re.I):
        return None
    return value


def has_place_reference(query):
    return bool(re.search(PLACE_REFERENCE, query, re.I))


def resolve_query(query, session, *, topic_reference=False):
    previous = session.conversation_context
    if topic_reference and previous.get("capability") in {"web.search", "knowledge.lookup"}:
        reference = previous.get("subject") or topic_subject(previous.get("topic_query") or previous.get("query") or "")
    elif topic_reference and previous.get("capability") == "weather.get_forecast":
        reference = previous.get("location") or destination(session).get("name")
    else:
        reference = destination(session).get("name") or previous.get("location")
    if reference:
        query = re.sub(r"\b(ở|tại) (?:đó|đấy|ấy)\b", lambda m: m[1] + " " + reference, query, flags=re.I)
        query = re.sub(r"\b(?:nơi|chỗ|thành phố|tỉnh) (?:đó|đấy|ấy)\b", lambda m: reference, query, flags=re.I)
    if re.search(r"\b(?:nơi sắp đến|chỗ sắp tới|nơi sắp tới)\b", query, re.I):
        upcoming = destination(session).get("name")
        if upcoming:
            query = re.sub(r"\b(?:nơi sắp đến|chỗ sắp tới|nơi sắp tới)\b", lambda m: upcoming, query, flags=re.I)
    value = normalize_text(query)
    if re.fullmatch(r"(?:con |the )?(?:ngay mai|ngay kia)(?: thi sao)?[?.! ]*", value):
        previous = session.conversation_context
        if previous.get("capability") == "weather.get_forecast":
            query = "Thời tiết " + ("ngày kia" if "ngay kia" in value else "ngày mai")
            if previous.get("location"):
                query += " tại " + previous["location"]
    return query


def search_followup_query(query, session):
    """Carry only the preceding search topic, never the full conversation."""
    previous = session.conversation_context
    topic = previous.get("topic_query") or previous.get("query")
    query = re.sub(r"^(?:ở|o|tại|tai) (?:đó|do)\s+", "", query, flags=re.I)
    return f"{query.rstrip(' .!?')} (chủ đề: {topic[:400]})" if topic else query


def search_topic(query, session):
    previous = session.conversation_context
    topic = previous.get("topic_query") or previous.get("query")
    if previous.get("capability") == "web.search" and topic and query.endswith(f"(chủ đề: {topic[:400]})"):
        return topic
    return query
