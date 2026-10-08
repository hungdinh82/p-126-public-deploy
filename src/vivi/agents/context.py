"""Resolve a bounded dialogue referent or a pending slot, before tool selection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.vivi.agents.slots import cabin_zone, is_cabin_zone_reply
from src.vivi.rag.knowledge import BY_KEY, analyze_question, find_topic
from src.vivi.text import normalize_text


@dataclass(frozen=True)
class ResolvedRequest:
    original: str
    text: str
    topic: str | None
    source: str = "current_turn"

    def as_dict(self) -> dict:
        return {"topic": self.topic, "source": self.source, "resolved_query": self.text}


def resolve_request(query: str, history: list[dict]) -> ResolvedRequest:
    topic = find_topic(query)
    text = normalize_text(query).strip(" .?!")
    if not history:
        return ResolvedRequest(query, query, topic.key if topic else None)
    last = history[-1]
    previous = str(last.get("query", ""))
    answer = normalize_text(str(last.get("answer", "")))
    # Fill only the slot that was just requested. New full requests never inherit it.
    # New turns use typed tasks with expiry. Prose parsing is compatibility only
    # for history created before the task checkpoint existed.
    if last.get("route") == "clarify":
        if re.fullmatch(r"(?:sac )?(?:ac|dc|sac nhanh|sac cham)", text) and "sac" in normalize_text(previous):
            return ResolvedRequest(query, f"{previous} sạc {query}", "ev_charging", "pending_charger")
    if last.get("route") == "clarify" and "pending_task" not in last.get("diagnostics", {}):
        if re.fullmatch(r"\d+(?:[.,]\d+)?(?: do)?", text) and "nhiet do" in answer:
            return ResolvedRequest(query, f"đặt nhiệt độ {query} độ", "climate", "pending_temperature")
        if cabin_zone(text) and is_cabin_zone_reply(text) and re.search(r"ben nao|vi tri nao", answer):
            return ResolvedRequest(
                query, f"{previous} {query}", find_topic(previous).key if find_topic(previous) else None, "pending_zone"
            )
    if topic:
        return ResolvedRequest(query, query, topic.key)
    if (
        last.get("intent") == "climate.set_temperature"
        or (last.get("route") == "action" and find_topic(previous) and find_topic(previous).key == "climate")
    ) and re.fullmatch(r"(?:tang|giam|ha|them|bot)(?: them| bot)? \d+(?:[.,]\d+)?(?: do)?", text):
        return ResolvedRequest(query, f"{query} độ nhiệt độ", "climate", "action_follow_up")
    referential = re.search(r"\b(no|day|do|ay)\b|tinh nang.*khac", text)
    continuation = re.search(r"\b(vay|the|con)\b", text)
    # Look only at the most recent knowledge turn; unrelated intervening actions
    # terminate knowledge context instead of resurrecting an older topic.
    if last.get("route") == "handbook" and (referential or continuation):
        diagnostics = last.get("diagnostics") or {}
        key = diagnostics.get("request", {}).get("topic")
        previous_query = diagnostics.get("retrieval_query") or previous
        previous_topic = BY_KEY.get(key) or analyze_question(previous_query).topic
        if previous_topic:
            return ResolvedRequest(query, f"{query} {previous_topic.label}", previous_topic.key, "knowledge_follow_up")
    return ResolvedRequest(query, query, None)
