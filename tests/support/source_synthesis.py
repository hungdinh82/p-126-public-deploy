"""Shared component fixtures extracted from historical scenario modules."""

import json

from src.vivi.conversation.service import ConversationService
from src.vivi.dialogue.planner import ModelProvider


def source(identifier, content):
    return {
        "source_id": identifier,
        "title": "Du lịch TEST",
        "content": content,
        "source_url": f"https://example.org/{identifier}",
        "kind": "web",
    }


class ReferenceClient:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    async def agenerate_json(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(next(self.answers), ensure_ascii=False)


def composer(client):
    return ConversationService({"google": ModelProvider("google", client, 1)})
