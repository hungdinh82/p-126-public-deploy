from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

from src.rag.schemas import RetrievedChunk
from src.vivi.contracts import ActionProposal, VehicleState


class SpeechToTextPort(Protocol):
    async def transcribe(self, path: Path) -> str: ...


class TextToSpeechPort(Protocol):
    async def synthesize(self, text: str) -> bytes: ...
    def stream(self, text: str) -> AsyncIterator[bytes]: ...


class LanguageModelPort(Protocol):
    name: str
    async def propose(self, transcript: str, vehicle: VehicleState) -> ActionProposal: ...


class HandbookPort(Protocol):
    def retrieve(
        self, query: str, vehicle_model: str, model_year: int, locale: str
    ) -> list[RetrievedChunk]: ...


class VehiclePort(Protocol):
    name: str
    async def get_state(self, session_id: str) -> VehicleState: ...
