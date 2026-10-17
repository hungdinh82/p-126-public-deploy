"""Version 2 wire contract: one explicit input kind and one navigation aggregate."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.vivi.dialogue.contracts import ActionDraft, QuestionReply
from src.vivi.domain.models import TurnRequest
from src.vivi.navigation.workflow import Position


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UtteranceInput(Input):
    kind: Literal["utterance"]
    text: str = Field(min_length=1, max_length=500)


class ActionInput(Input):
    kind: Literal["action"]
    action: ActionDraft

    @field_validator("action")
    @classmethod
    def canonical_capability(cls, value):
        if value.capability.startswith("trip."):
            raise ValueError("Use navigation capabilities with purpose=itinerary")
        return value


class AnswerInput(Input):
    kind: Literal["question_reply"]
    answer: QuestionReply


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)
    turn_id: str = Field(min_length=1, max_length=100)
    input: Annotated[UtteranceInput | ActionInput | AnswerInput, Field(discriminator="kind")]
    provider: Literal["google", "openai", "openrouter", "local"] | None = None
    position: Position | None = None

    def request(self):
        content = self.input
        return TurnRequest(
            session_id=self.session_id,
            turn_id=self.turn_id,
            transcript=content.text
            if isinstance(content, UtteranceInput)
            else "Câu trả lời"
            if isinstance(content, AnswerInput)
            else "Thao tác trên màn hình",
            action=content.action if isinstance(content, ActionInput) else None,
            question_reply=content.answer if isinstance(content, AnswerInput) else None,
            llm_provider=self.provider,
            position=self.position.model_dump(mode="json") if self.position else None,
        )


def envelope(response):
    value = response.model_dump(mode="json") if hasattr(response, "model_dump") else dict(response)
    value.pop("navigation", None)
    value.pop("trip_plan", None)
    value["contract_version"] = 2
    return value
