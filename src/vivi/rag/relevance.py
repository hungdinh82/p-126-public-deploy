"""Explicit variant constraints that similarity search must not conflate."""

import re

from src.vivi.rag.schemas import RetrievedChunk
from src.vivi.text import normalize_text


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
