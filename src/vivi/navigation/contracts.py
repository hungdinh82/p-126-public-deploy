"""Destination slots and evidence independent of dialogue and map adapters."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.vivi.navigation.destinations import region_for
from src.vivi.text import normalize_text


class AreaConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200)
    source: Literal["user", "answer", "context", "position", "hypothesis"] = "user"
    constraint: Literal["required", "preferred"] = "required"
    question_id: str | None = None

    @model_validator(mode="after")
    def inferred_area(self):
        if self.source in {"hypothesis", "position"}:
            self.constraint = "preferred"
        return self


class DestinationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    raw_text: str = Field(default="", max_length=500)
    name: str | None = Field(default=None, max_length=200)
    entity_type: str | None = Field(default=None, max_length=50)
    area: AreaConstraint | None = None
    selection: Literal["named_place", "category", "region", "reference"] = "named_place"
    qualifiers: list[str] = Field(default_factory=list, max_length=10)
    search_hypotheses: list[str] = Field(default_factory=list, max_length=2)
    reference: str | None = None
    nearest: bool = False

    @classmethod
    def from_arguments(cls, arguments):
        if arguments.get("destination"):
            return cls.model_validate(arguments["destination"])
        query = arguments.get("query") or ""
        parts = [p.strip() for p in query.split(",") if p.strip()]
        category = arguments.get("category")
        selection = "category" if category else "region" if arguments.get("destination_scope") == "region" else "named_place"
        return cls(raw_text=query, name=parts[0] if parts else None,
                   area=AreaConstraint(text=", ".join(parts[1:])) if len(parts) > 1 else None,
                   selection=selection, entity_type=category or entity_type(query),
                   nearest=bool(arguments.get("nearest")))

    def with_area(self, text, *, question_id=None):
        return self.model_copy(update={"area": AreaConstraint(text=text, source="answer", question_id=question_id)})

    def query(self):
        name = self.name
        if not name and self.selection == "category":
            name = self.raw_text or {"pharmacy": "nhà thuốc", "charging": "trạm sạc",
                                     "restaurant": "nhà hàng", "cafe": "quán cà phê"}.get(self.entity_type, self.entity_type)
        if name:
            name = " ".join([name, *(q for q in self.qualifiers if normalize_text(q) not in normalize_text(name))])
        return ", ".join(p for p in [name, self.area.text if self.area else None] if p) if name else ""

    def complete(self):
        if self.selection == "category":
            return bool(self.entity_type or self.name or self.raw_text)
        if self.selection == "reference":
            return bool(self.reference)
        if not self.name:
            return False
        value = normalize_text(self.name)
        return self.selection == "region" or bool(region_for(self.name)) or value not in {
            "quan ca phe", "quan pho", "nha hang", "khach san", "nha thuoc", "hieu thuoc", "tram sac",
            "nha toi", "nha minh", "do", "day", "cho do",
        }


def entity_type(name):
    value = normalize_text(name or "")
    if re.match(r"^đèn\b", (name or "").lower()):
        return None
    for pattern, kind in [
        (r"^(?:pho|duong|ngo|hem)\b", "street"),
        (r"^(?:truong )?(?:tieu hoc|trung hoc|dai hoc|mam non)\b", "school"),
        (r"^ho\b", "lake"), (r"^quang truong\b", "square"), (r"^cong vien\b", "park"),
        (r"^den\b", "temple"), (r"^lang\b", "mausoleum"), (r"^ga\b", "station"),
    ]:
        if re.search(pattern, value):
            return kind
    return None


class PlaceResolution(BaseModel):
    status: Literal["resolved", "ambiguous", "missing_constraints", "no_verified_match", "provider_unavailable"]
    candidates: list[dict] = Field(default_factory=list)
    selected_handle: str | None = None
    reason: str
    required_slot: str | None = None
    evidence: list[dict] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
