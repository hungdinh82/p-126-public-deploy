"""Log 997–1001: duplicate map records, semantic decision, unresolved start."""

import json
from types import SimpleNamespace

import pytest

from src.vivi.navigation.presentation import choice_message
from src.vivi.navigation.selection import decide_place, distinct_places
from tests.support.long_term_memory import services as services


def station(handle, lng=106.68728, suffix=""):
    return dict(
        handle=handle,
        name="Ga Hải Phòng Phố Lương Khánh Thiện" + suffix,
        address="Phường Gia Viên, Thành phố Hải Phòng",
        lat=20.85616,
        lng=lng,
    )


def test_log_1001_deduplicates_vendor_records_but_not_different_locations():
    candidates = distinct_places([station("a"), station("b", 106.68750, " Lương Khánh Thiện Ngô Quyền Hải Phòng")])
    assert len(candidates) == 1
    assert choice_message(candidates) == "Ga Hải Phòng, Phường Gia Viên. Đúng chỗ này chứ?"
    assert len(distinct_places([station("a"), station("b", 106.70)])) == 2
    assert len(distinct_places([station("a", suffix=" cửa Đông"), station("b", suffix=" cửa Tây")])) == 2


class DecisionModel:
    def __init__(self, reply=None):
        self.reply = reply

    async def agenerate_json(self, **kwargs):
        if kwargs["schema_name"] == "destination_choice":
            if self.reply is not None:
                return json.dumps(self.reply)
            candidate = json.loads(kwargs["user"])["candidates"][0]
            return json.dumps(dict(mode="propose", handle=candidate["handle"], reason="exact_destination"))
        return json.dumps(
            {"act": "request", "actions": [{"capability": "navigation.plan", "arguments": {"query": "Tràng An"}}]}
        )


@pytest.mark.parametrize(
    "reply",
    [
        dict(mode="propose", handle="invented", reason="exact_destination"),
        dict(mode="propose", handle="a", reason="nearest_requested"),
        dict(mode="propose", handle="a", reason="ambiguous"),
        {"garbage": True},
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_judgment_falls_back_to_clarification(reply):
    resolver = SimpleNamespace(providers={"google": SimpleNamespace(client=DecisionModel(reply))})
    result = await decide_place(resolver, "google", "Ga Hải Phòng", [station("a")])
    assert result["mode"] == "clarify" and result["source"] == "fallback"


@pytest.mark.asyncio
async def test_valid_ambiguous_model_decision_keeps_question():
    resolver = SimpleNamespace(
        providers={
            "google": SimpleNamespace(client=DecisionModel(dict(mode="clarify", handle=None, reason="ambiguous")))
        }
    )
    result = await decide_place(resolver, "google", "Ga Hải Phòng", [station("a"), station("b", 106.70)])
    assert result["mode"] == "clarify" and result["source"] == "evidence"
