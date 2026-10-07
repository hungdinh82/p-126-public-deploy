"""Compatibility facade over the question-aware evidence selector."""
from src.vivi.rag.evidence import EvidenceSelector
from src.vivi.rag.schemas import RetrievedChunk


def select_excerpts(query: str, chunks: list[RetrievedChunk], max_characters: int) -> list[tuple[str, RetrievedChunk]]:
    return [(unit.text, unit.source) for unit in EvidenceSelector().select(query, chunks, max_characters)]
