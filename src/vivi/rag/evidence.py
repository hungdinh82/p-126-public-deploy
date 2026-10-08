"""Question-aware evidence units used by the offline answer composer.

A section being on-topic is not sufficient: a definition needs an explanation,
a duration needs a time, and a specification needs the requested numeric row.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from src.vivi.rag.knowledge import KnowledgeQuestion, analyze_question
from src.vivi.rag.query import expand_query
from src.vivi.rag.relevance import matches_topic, matches_variant
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.text import normalize_text

_STOP = set(
    "ban minh toi xe vf8 cua co la gi nao the nhu sao bao nhieu hay va voi ve cho mot nhung cac nay do khi o duoc de hien tai thong tin tinh nang huong dan su dung cach can hay khong ay day gioi thieu biet con".split()
)


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_text(text))) - _STOP


@lru_cache(maxsize=4096)
def evidence_units(content: str) -> tuple[str, ...]:
    lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
    if len(lines) > 2 and normalize_text(lines[0]).startswith("mo ta chi tiet"):
        lines = lines[1:]
    seen, units = set(), []
    for line in lines:
        for unit in re.split(r"(?<=[.!?])\s+", line):
            unit = re.sub(r"^(?:LƯU Ý|CẢNH BÁO|THẬN TRỌNG)\s*", "", unit).strip()
            key = normalize_text(unit).strip(" .")
            if not key or key in seen or key in {"mo ta chi tiet", "description", "chi tiet"} or unit.endswith(":"):
                continue
            if not re.search(r"[.!?]$|\d", unit) and not re.match(
                r"(?:nhan|cham|dat|mo|dong|thao|lap|kiem tra|dung)\b", key
            ):
                continue
            seen.add(key)
            units.append(unit)
    return tuple(units)


def explains_topic(unit: str, question: KnowledgeQuestion) -> bool:
    text = normalize_text(unit)
    if not question.topic or not re.search(question.topic.evidence, text):
        return False
    # The subject of the explanation must be the requested feature. A paragraph
    # explaining LDW may mention LKA as a dependency, but does not define LKA.
    subject = re.split(r"\bla (?:mot |he thong|chuc nang)|\bgiup\b|\bcho phep\b|\bngan\b", text, maxsplit=1)
    return (
        len(subject) == 2
        and bool(re.search(question.topic.evidence, subject[0]))
        and not re.search(r"^nhan|^cham|^khi |^neu |^khong ", text)
    )


@dataclass(frozen=True)
class EvidenceUnit:
    text: str
    source: RetrievedChunk
    score: float
    chunk_order: int
    unit_order: int


class EvidenceSelector:
    def select(self, query: str, chunks: list[RetrievedChunk], max_characters: int) -> list[EvidenceUnit]:
        question = analyze_question(query)
        terms, primary = words(expand_query(query)), words(query)
        attribute = next(
            (
                phrase
                for phrase in ("kich thuoc", "dung luong", "dien ap", "ap suat", "cong suat")
                if phrase in normalize_text(query)
            ),
            None,
        )
        candidates = []
        for chunk_rank, chunk in enumerate(chunks):
            if not matches_variant(query, chunk) or not matches_topic(query, chunk):
                continue
            heading = " ".join(chunk.section_path)
            for unit_rank, unit in enumerate(evidence_units(chunk.content)):
                text = normalize_text(unit)
                if len(unit) > max_characters or re.search(r"^xem |^de biet (?:them )?thong tin|^tham khao", text):
                    continue
                if question.kind == "artifact" and not re.search(r"ma nguon|source code", text):
                    continue
                if question.kind == "duration" and not re.search(
                    r"\d+(?:[.,]\d+)?\s*(?:gio|phut|giay)|thoi gian (?:sac |con lai)|sac.*(?:phut|gio)", text
                ):
                    continue
                explanatory = explains_topic(unit, question)
                if (
                    question.kind in {"definition", "availability"}
                    and question.topic
                    and not explanatory
                    and not (question.kind == "definition" and re.search(r"^de (?:tranh|bao ve)|se bi|se duoc", text))
                ):
                    continue
                if question.kind == "limitation" and not re.search(r"khong|han che|nguoi lai.*trach nhiem", text):
                    continue
                if question.kind == "troubleshooting" and not re.search(r"khong|kiem tra|loi|ket|thu lai", text):
                    continue
                if attribute and (
                    attribute not in text
                    or not re.search(r"\d", unit)
                    or text.lstrip("(").startswith(("neu ", "khi ", "de ", "khong ", "vi du", "gia su"))
                ):
                    continue
                if primary and not explanatory and len(primary & (words(unit) | words(heading))) < min(2, len(primary)):
                    continue
                overlap = len(terms & words(unit))
                if overlap == 0:
                    continue
                score = (overlap + 0.15 * len(terms & words(heading))) / math.sqrt(max(8, len(unit.split())))
                if explanatory:
                    score += 2.0
                if attribute and text.startswith(attribute):
                    score += 1.0
                candidates.append(EvidenceUnit(unit, chunk, score, chunk_rank, unit_rank))
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.chunk_order, candidate.unit_order))
        if not candidates:
            return []
        best = candidates[0]
        selected = [best]
        if question.kind not in {"definition", "availability", "categorical"} and not attribute:
            # Prefer adjacent procedural steps in their source order. Never join
            # unrelated sources merely because they have similar vocabulary.
            for candidate in candidates[1:]:
                if (
                    candidate.source.source_id == best.source.source_id
                    and candidate.score >= best.score * 0.65
                    and len(best.text) + len(candidate.text) + 1 <= max_characters
                ):
                    selected.append(candidate)
                    break
            if question.kind == "procedure":
                selected.sort(key=lambda candidate: candidate.unit_order)
        return selected
