from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ManualSearchArguments(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    vehicle_model: str = Field(default="VF8", min_length=2, max_length=20)
    model_year: int = Field(default=2026, ge=2000, le=2100)
    locale: str = Field(default="vi_vn", pattern=r"^[a-z]{2,10}_[a-z]{2,10}$")


class ModelDecision(BaseModel):
    """Validated JSON contract received from the upstream language model."""

    intent: Literal["manual.search"]
    arguments: ManualSearchArguments
    needs_clarification: bool = False
    clarification_question: str | None = None
    spoken_response: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def reject_clarification(self) -> ModelDecision:
        if self.needs_clarification:
            raise ValueError("manual.search input must already contain a complete query")
        return self


class HandbookChunk(BaseModel):
    source_id: str
    document_id: str
    source_url: str
    vehicle_model: str
    model_year: int
    locale: str
    chapter_id: int
    chapter: str
    section_path: list[str]
    content_type: str
    content: str
    checksum: str
    chunk_index: int



class RetrievedChunk(HandbookChunk):
    semantic_distance: float = 1.0
    lexical_score: float = 0.0
    fused_score: float = 0.0


class Claim(BaseModel):
    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class Citation(BaseModel):
    source_id: str
    title: str
    section_path: list[str]
    source_url: str


class GroundedAnswer(BaseModel):
    answer: str = ""
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    insufficient_evidence: bool = False
    abstain_reason: str | None = None

    @model_validator(mode="after")
    def require_reason_or_answer(self) -> GroundedAnswer:
        if self.insufficient_evidence and not self.abstain_reason:
            raise ValueError("abstain_reason is required when evidence is insufficient")
        if not self.insufficient_evidence and not self.answer.strip():
            raise ValueError("answer is required when evidence is sufficient")
        return self


def model_decision_json_schema() -> dict[str, Any]:
    return ModelDecision.model_json_schema()
