from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator

from .adapters.llm import LLMAdapter
from .data_store import DataStore
from .safety import validate
from .schemas import ActionProposal, TurnRequest, TurnResponse
from .vehicle import VehicleSimulator


class Orchestrator:
    def __init__(self, llm: LLMAdapter, vehicle: VehicleSimulator, store: DataStore):
        self.llm = llm
        self.vehicle = vehicle
        self.store = store

    async def run(self, request: TurnRequest, llm: LLMAdapter | None = None) -> TurnResponse:
        llm = llm or self.llm
        started = time.perf_counter()
        vehicle = self.vehicle.state_for(request.session_id, request.vehicle_state)
        llm_started = time.perf_counter()
        proposal = await llm.propose(request.transcript, vehicle)
        llm_ms = (time.perf_counter() - llm_started) * 1000
        response = await self._finish(request, llm, vehicle, proposal, started, llm_ms)
        return response

    async def run_stream(self, request: TurnRequest, llm: LLMAdapter | None = None) -> AsyncIterator[dict]:
        """Stream safe speech segments, followed by the authoritative turn."""
        llm = llm or self.llm
        started = time.perf_counter()
        vehicle = self.vehicle.state_for(request.session_id, request.vehicle_state)
        llm_started = time.perf_counter()
        document = ""
        spoken_seen = ""
        pending_speech = ""
        streamed_speech = False

        async for delta in llm.stream_json(request.transcript, vehicle):
            document += delta
            intent = self._partial_intent(document)
            # Never stream a vehicle-action acknowledgement before safety and
            # the vehicle adapter have verified the action.
            if intent != "conversation.respond":
                continue
            spoken = self._partial_json_string(document, "spoken_response")
            if len(spoken) <= len(spoken_seen):
                continue
            pending_speech += spoken[len(spoken_seen):]
            spoken_seen = spoken
            segments, pending_speech = self._take_speech_segments(pending_speech)
            for segment in segments:
                streamed_speech = True
                yield {"type": "speech", "text": segment}

        llm_ms = (time.perf_counter() - llm_started) * 1000
        proposal = ActionProposal.model_validate_json(document)
        response = await self._finish(request, llm, vehicle, proposal, started, llm_ms)
        if proposal.intent == "conversation.respond" and pending_speech.strip():
            streamed_speech = True
            yield {"type": "speech", "text": pending_speech.strip()}
        yield {"type": "final", "streamed_speech": streamed_speech, "response": response.model_dump(mode="json")}

    async def _finish(self, request, llm, vehicle, proposal, started, llm_ms) -> TurnResponse:
        safety = validate(proposal, vehicle)
        if safety.allowed:
            if proposal.intent == "conversation.respond":
                message = proposal.spoken_response
            else:
                vehicle, message = await self.vehicle.execute(request.session_id, request.turn_id, proposal)
            status = "verified"
        else:
            message, status = safety.message, safety.status
        response = TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            transcript=request.transcript,
            provider=llm.name,
            status=status,
            action=proposal,
            vehicle_state=vehicle,
            message=message,
            latency_ms={"llm": round(llm_ms, 2), "total": round((time.perf_counter() - started) * 1000, 2)},
        )
        self.store.append_event(response.model_dump())
        return response

    @staticmethod
    def _partial_intent(document: str) -> str | None:
        match = re.search(r'"intent"\s*:\s*"([^"\\]+)"', document)
        return match.group(1) if match else None

    @staticmethod
    def _partial_json_string(document: str, key: str) -> str:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"', document)
        if not match:
            return ""
        start = match.end()
        escaped = False
        end = len(document)
        for index in range(start, len(document)):
            char = document[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                end = index
                break
        raw = document[start:end]
        # Streaming may split an escape sequence. Decode only the complete
        # prefix; the next delta will supply the remainder.
        while raw:
            try:
                return json.loads(f'"{raw}"')
            except json.JSONDecodeError:
                raw = raw[:-1]
        return ""

    @staticmethod
    def _take_speech_segments(text: str) -> tuple[list[str], str]:
        segments: list[str] = []
        while True:
            sentence = re.search(r"^(.+?[.!?…])(?:\s+|$)", text, re.S)
            clause = re.search(r"^(.{45,}?[,:;])(?:\s+|$)", text, re.S)
            match = sentence or clause
            if match:
                segments.append(match.group(1).strip())
                text = text[match.end():]
                continue
            if len(text) >= 100:
                split = text.rfind(" ", 0, 90)
                if split > 40:
                    segments.append(text[:split].strip())
                    text = text[split + 1:]
                    continue
            break
        return segments, text
