"""Short, query-focused literal evidence selection for the offline rules profile.

This selects complete source sentences/rows; it is not an SLM summarizer or an
entailment model. Weak word overlap is rejected rather than presenting a whole
unrelated handbook section as an answer.
"""

import math
import re

from src.vivi.rag.query import expand_query
from src.vivi.rag.relevance import matches_variant
from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.text import normalize_text

_STOP = set("ban minh toi xe vf8 cua co la gi nao the nhu sao bao nhieu hay va voi ve cho mot nhung cac nay do khi o duoc de hien tai thong tin tinh nang huong dan su dung cach can hay khong".split())


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_text(text))) - _STOP


def _units(content: str) -> list[str]:
    lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
    # Parser exports both the flattened HTML table and its individual rows.
    if len(lines) > 2 and normalize_text(lines[0]).startswith("mo ta chi tiet"):
        lines = lines[1:]
    seen = set()
    units = []
    for line in lines:
        for unit in re.split(r"(?<=[.!?])\s+", line):
            unit = re.sub(r"^(?:LƯU Ý|CẢNH BÁO|THẬN TRỌNG)\s*", "", unit).strip()
            key = normalize_text(unit).strip(" .")
            if key in seen or key in {"mo ta chi tiet", "description", "chi tiet"} or not key or unit.endswith(":"):
                continue
            if not re.search(r"[.!?]$|\d", unit) and not re.match(
                r"(?:Nhan|Cham|Dat|Mo|Dong|Thao|Lap|Kiem tra|Dung)\b", normalize_text(unit), re.I
            ):
                continue
            seen.add(key)
            units.append(unit)
    return units


def select_excerpts(query: str, chunks: list[RetrievedChunk], max_characters: int) -> list[tuple[str, RetrievedChunk]]:
    terms = _words(expand_query(query))
    ranked = []
    attribute = next((phrase for phrase in ("kich thuoc", "dung luong", "dien ap", "ap suat", "cong suat")
                      if phrase in normalize_text(query)), None)
    for chunk_rank, chunk in enumerate(chunks):
        if not matches_variant(query, chunk):
            continue
        heading = " ".join(chunk.section_path)
        heading_overlap = len(terms & _words(heading))
        for index, unit in enumerate(_units(chunk.content)):
            normalized = normalize_text(unit)
            overlap = len(terms & _words(unit))
            if attribute and (attribute not in normalized or not re.search(r"\d", unit)
                              or normalized.lstrip("(").startswith(("neu ", "khi ", "de ", "khong ", "vi du", "gia su"))):
                continue
            if "cruise control" in normalize_text(query) and not re.search(r"cruise|hanh trinh|\bacc\b", normalized):
                continue
            if re.search(r"khong (?:mo|dong|hoat dong).*duoc|bi ket", normalize_text(query)) and not re.search(
                r"khong|kiem tra|loi|ket|thu lai", normalized
            ):
                continue
            if overlap == 0 or normalized.startswith("xem "):
                continue
            # Complete units preserve numbers, variant conditions and negation.
            if len(unit) > max_characters:
                continue
            score = (overlap + 0.15 * heading_overlap) / math.sqrt(max(8, len(unit.split())))
            if attribute and normalized.startswith(attribute):
                score += 1.0
            ranked.append((score, chunk_rank, index, unit, chunk))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    if not ranked:
        return []
    best = ranked[0]
    selected = [(best[3], best[4])]
    # One source and at most two complete units, instead of joining unrelated sections.
    if not attribute:
        for score, _, _, unit, chunk in ranked[1:]:
            if chunk.source_id != best[4].source_id or normalize_text(unit) == normalize_text(best[3]):
                continue
            if score < best[0] * 0.65:
                continue
            if len(best[3]) + 1 + len(unit) <= max_characters:
                selected.append((unit, chunk))
                break
    return selected
