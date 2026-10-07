"""Bounded, source-backed voice composition; no per-utterance canned RAG facts."""

from __future__ import annotations

import re

from src.vivi.rag.knowledge import TOPICS, analyze_question
from src.vivi.rag.schemas import GroundedAnswer, RetrievedChunk
from src.vivi.text import normalize_text


def grounded_answer(pieces: list[tuple[str, RetrievedChunk]]) -> GroundedAnswer:
    by_id = {chunk.source_id: chunk for _, chunk in pieces}
    return GroundedAnswer(
        answer=" ".join(text for text, _ in pieces),
        claims=[{"text": text, "source_ids": [chunk.source_id]} for text, chunk in pieces],
        citations=[
            {
                "source_id": chunk.source_id,
                "title": chunk.chapter,
                "section_path": chunk.section_path,
                "source_url": chunk.source_url,
            }
            for chunk in by_id.values()
        ],
    )


def feature_overview(query: str, chunks: list[RetrievedChunk], max_characters: int) -> GroundedAnswer | None:
    question = analyze_question(query)
    if (
        not question.topic
        or not question.topic.umbrella
        or question.kind not in {"definition", "overview", "availability", "general"}
    ):
        return None
    features = []
    for topic in TOPICS:
        if topic.umbrella or topic.group != question.topic.group:
            continue
        for chunk in chunks:
            heading, text = (
                normalize_text(chunk.section_path[-1] if chunk.section_path else chunk.chapter),
                normalize_text(chunk.content),
            )
            if not question.topic.matches(heading) or not topic.matches(text):
                continue
            if re.search(r"^canh bao|^than trong|^luu y", text):
                continue
            features.append((topic, chunk))
            break
    if not features:
        return None
    # Names are vocabulary labels. Presence is established only by retrieved
    # positive feature descriptions, never by remembered user claims.
    first = features[0][0]
    intro = f"{question.topic.label} gồm các tính năng như {first.label}"
    for topic, chunk in features[1:3]:
        intro += f", {topic.label}"
    intro = "Tuỳ phiên bản, " + intro[0].lower() + intro[1:] + "."
    # Each source supports the actual features it names, rather than assigning
    # an entire generated list to a single convenient citation.
    ids = list(dict.fromkeys(chunk.source_id for _, chunk in features[:3]))
    warning = next(
        (
            chunk
            for chunk in chunks
            if "khong the thay the" in normalize_text(chunk.content)
            and re.search(question.topic.evidence, normalize_text(" ".join(chunk.section_path)))
        ),
        None,
    )
    answer = intro
    claims = [{"text": intro, "source_ids": ids}]
    used = {chunk.source_id: chunk for _, chunk in features[:3]}
    if warning:
        text = "Các hỗ trợ này không thay thế sự chú ý và kiểm soát của người lái."
        if len(answer) + len(text) + 1 <= max_characters:
            answer += " " + text
            claims.append({"text": text, "source_ids": [warning.source_id]})
            used[warning.source_id] = warning
    if len(answer) > max_characters:
        return None
    return GroundedAnswer(
        answer=answer,
        claims=claims,
        citations=[
            {
                "source_id": chunk.source_id,
                "title": chunk.chapter,
                "section_path": chunk.section_path,
                "source_url": chunk.source_url,
            }
            for chunk in used.values()
        ],
    )


def compose_excerpt(query: str, selected: list[tuple[str, RetrievedChunk]], max_characters: int) -> GroundedAnswer:
    question = analyze_question(query)
    pieces = [(text.rstrip(" .") + ".", chunk) for text, chunk in selected]
    if question.kind == "availability" and question.topic:
        text, chunk = pieces[0]
        qualifier = f"Trang bị {question.topic.label} tuỳ phiên bản."
        conditional = re.search(
            r"neu duoc trang bi|tuy phien ban|tuy chon",
            normalize_text(" ".join(chunk.section_path) + " " + chunk.content),
        )
        if conditional and len(text) + len(qualifier) + 1 <= max_characters:
            # Conditional presence rather than claiming this particular car is equipped.
            pieces[0] = (f"{qualifier} {text}", chunk)
    return grounded_answer(pieces)
