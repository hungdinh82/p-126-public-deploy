"""Explicit variant constraints that similarity search must not conflate."""

import re
from functools import lru_cache

from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.text import normalize_text as _normalize_text


@lru_cache(maxsize=4096)
def normalize_text(text: str) -> str:
    """Normalize static chunk text once instead of rescanning the handbook per turn."""
    return _normalize_text(text)


def variant_filter(query: str):
    text = normalize_text(query)
    excluded = []
    if "lop" in text and re.search(r"kich thuoc|thong so|loai lop", text) and "du phong" not in text:
        excluded.append("du phong")
    for requested, other in (("sdi", "catl"), ("catl", "sdi")):
        if requested in text:
            excluded.append(other)
    if not excluded:
        return lambda chunk: True

    def accept(chunk: RetrievedChunk) -> bool:
        heading = normalize_text(" ".join(chunk.section_path))
        return not any(variant in heading for variant in excluded)

    return accept


def matches_variant(query: str, chunk: RetrievedChunk) -> bool:
    return variant_filter(query)(chunk)


def is_definition(query: str) -> bool:
    from src.vivi.rag.knowledge import analyze_question

    return analyze_question(query).kind == "definition"


def topic_pattern(query: str) -> str | None:
    from src.vivi.rag.knowledge import analyze_question

    topic = analyze_question(query).topic
    return topic.evidence if topic else None


def matches_topic(query: str, chunk: RetrievedChunk) -> bool:
    from src.vivi.rag.knowledge import analyze_question

    question = analyze_question(query)
    text = normalize_text(query)
    heading = normalize_text(" ".join(chunk.section_path))
    content = normalize_text(chunk.content)
    counted = re.search(
        r"\bco (?:tat ca |tong cong )?bao nhieu\s+(.+?)(?=\s+(?:de|tren|cua|trong|cho|khi)\b|[?!]|$)", text
    )
    if counted and len(counted[1].split()) >= 2 and counted[1].strip() not in heading + " " + content:
        return False
    if question.topic and question.topic.key == "ev_charging" and re.search(r"dien thoai|khong day|usb", heading):
        return False
    # Definitions/equipment need an explicit feature anchor. For procedures and
    # specifications, semantic retrieval may find a valid implicit subject;
    # topic headings rerank those candidates rather than deleting them early.
    if question.topic and question.kind in {"definition", "availability"}:
        return bool(re.search(question.topic.evidence, heading + " " + content))
    return True


def rerank_score(query: str, chunk: RetrievedChunk) -> float:
    """Rank question-compatible evidence; no source-id-specific boosts."""
    from src.vivi.rag.knowledge import analyze_question

    question = analyze_question(query)
    heading = normalize_text(" ".join(chunk.section_path))
    content = normalize_text(chunk.content)
    score = chunk.fused_score
    if question.topic and re.search(question.topic.evidence, heading):
        score += 0.02
    if question.kind in {"definition", "availability", "overview"}:
        if (
            question.topic
            and re.search(question.topic.evidence, content)
            and re.search(r"\bla (?:mot |he thong|chuc nang)|\bgiup\b|\bcho phep\b", content)
        ):
            score += 0.04
        if re.search(r"tong quan|gioi thieu|he thong.*(?:neu duoc trang bi|\()", heading):
            score += 0.025
        if (
            question.topic
            and question.topic.umbrella
            and re.search(question.topic.evidence, heading)
            and re.search(r"kich hoat|bao gom|cac tinh nang", content)
        ):
            score += 0.035
        if re.search(r"^canh bao|^than trong", content):
            score -= 0.025
    elif question.kind == "procedure" and re.search(r"cach|su dung|van hanh|hoat dong", heading):
        score += 0.02
    return score
