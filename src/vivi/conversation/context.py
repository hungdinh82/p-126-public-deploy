"""Small, typed-by-owner views of navigation and conversational references."""

import re

from src.vivi.text import normalize_text


def destination(session):
    workspace = session.navigation_workspace
    # A running journey takes precedence over an unaccepted replacement draft.
    owner = workspace.journey or workspace.draft
    selected = (owner.selected or {}) if owner else {}
    if not selected and session.trip_plan:
        selected = session.trip_plan.selected or {}
    return {key: selected[key] for key in ("name", "address", "lat", "lng", "handle") if key in selected}


def conversation_view(session):
    return {"destination": destination(session), "recent_topic": session.conversation_context}


def resolve_query(query, session):
    reference = destination(session).get("name") or session.conversation_context.get("location")
    if reference:
        query = re.sub(r"\b(ở|tại) đó\b", lambda m: m[1] + " " + reference, query, flags=re.I)
        query = re.sub(r"\b(?:nơi sắp đến|chỗ sắp tới|nơi sắp tới)\b", lambda m: reference, query, flags=re.I)
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
