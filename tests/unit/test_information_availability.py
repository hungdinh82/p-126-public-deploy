"""Regressions from real turns 1341–1352: intent, references and availability."""

import asyncio
import json
import time

import httpx
import pytest

from src.vivi.conversation.context import resolve_query
from src.vivi.conversation.service import ConversationService, equivalent_numeric_claims, synthesis_passages
from src.vivi.dialogue.contracts import SessionState
from src.vivi.information.policy import retrieval_query
from src.vivi.information.service import InformationService
from tests.support.open_world import configuration, event, handler
from tests.support.source_synthesis import ReferenceClient, composer, source


def information_state(query="Hà Nội có gì đặc sắc du lịch"):
    return SessionState(
        conversation_context={"focus": "information", "capability": "web.search", "query": query, "topic_query": query}
    )


@pytest.mark.parametrize("name", ["Paris", "Thành phố Sao Mai", "Đảo Mặt Trăng"])
def test_references_are_not_a_hardcoded_city_list(name):
    state = information_state(f"Giới thiệu về {name}")
    assert resolve_query("Dân số ở đấy là bao nhiêu?", state, topic_reference=True) == f"Dân số ở {name} là bao nhiêu?"


def test_ambiguous_previous_subject_is_not_guessed():
    state = information_state("Giới thiệu về Hà Nội và Hải Phòng")
    assert resolve_query("Dân số ở đấy là bao nhiêu?", state, topic_reference=True) == "Dân số ở đấy là bao nhiêu?"


@pytest.mark.asyncio
async def test_population_search_preserves_place_and_synthesis_goal(tmp_path):
    calls = []
    client = ReferenceClient([{"text": "Hà Nội có 8 triệu người theo số liệu trong nguồn.", "evidence_ids": ["e1"]}])

    def respond(request):
        calls.append(request)
        assert json.loads(request.content)["query"] == "Dân số ở Hà Nội là bao nhiêu?"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Dân số Hà Nội",
                        "url": "https://example.org/hanoi",
                        "content": "Hà Nội có 8 triệu người.",
                    }
                ]
            },
        )

    service = InformationService(configuration(tmp_path), composer(client), transport=httpx.MockTransport(respond))
    result = await service.execute(
        "web.search",
        {"query": "Dân số ở đấy là bao nhiêu?"},
        event(text="Dân số ở đấy là bao nhiêu?"),
        information_state(),
        provider="google",
    )
    assert result["status"] == "completed" and len(calls) == 1
    assert json.loads(client.calls[0]["user"])["question"] == "Dân số ở Hà Nội là bao nhiêu?"
    assert result["information_context"]["subject"] == "Hà Nội"


@pytest.mark.asyncio
async def test_unresolved_reference_never_searches_population_of_random_places(tmp_path):
    def no_network(request):
        pytest.fail("Unresolved place must not reach search")

    service = InformationService(
        configuration(tmp_path), ConversationService(), transport=httpx.MockTransport(no_network)
    )
    result = await service.execute(
        "web.search", {"query": "Dân số ở đấy là bao nhiêu?"}, event(), SessionState(), provider="google"
    )
    assert result["error_code"] == "information_reference_missing" and result["status"] == "blocked"
    assert not service.quota.path.exists()


@pytest.mark.asyncio
async def test_turn_budget_timeout_does_not_disable_healthy_model_for_next_turn():
    class RecoveringClient:
        calls = 0

        async def agenerate_json(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.2)
            return json.dumps({"text": "Tràng An có cảnh quan đẹp.", "evidence_ids": ["e1"]})

    client = RecoveringClient()
    model = composer(client)
    evidence = [source("a", "Tràng An có cảnh quan đẹp.")]
    first = await model.respond("Tràng An có gì?", provider="google", evidence=evidence, budget_seconds=0.02)
    assert first["error_code"] == "generation_timeout"
    assert first["generation_attempts"][0]["budget_limited"]
    second = await model.respond("Tràng An có gì?", provider="google", evidence=evidence, budget_seconds=1)
    assert second["status"] == "completed" and client.calls == 2


@pytest.mark.asyncio
async def test_unavailable_models_do_not_trigger_richer_search_or_grounding_error(tmp_path):
    calls = []
    client = ReferenceClient([])
    model = composer(client)
    model.gateway.providers["google"].unavailable_until = time.monotonic() + 30
    service = InformationService(configuration(tmp_path), model, transport=httpx.MockTransport(handler(calls)))
    result = await service.execute(
        "web.search", {"query": "Đà Nẵng có gì du lịch?"}, event(), SessionState(), provider="google"
    )
    assert result["error_code"] == "generation_unavailable" and len(calls) == 1 and client.calls == []
    assert len(result["retrieval_attempts"]) == 1 and result["citations"]


@pytest.mark.asyncio
async def test_connection_failure_still_opens_provider_circuit():
    class BrokenClient:
        calls = 0

        async def agenerate_json(self, **kwargs):
            self.calls += 1
            raise httpx.ConnectError("offline")

    client = BrokenClient()
    model = composer(client)
    for _ in range(2):
        result = await model.respond(
            "Tràng An có gì?", provider="google", evidence=[source("a", "Tràng An có cảnh quan đẹp.")]
        )
        assert result["error_code"] == "generation_unavailable"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_synthesis_can_use_remaining_budget_instead_of_arbitrary_five_second_cap(tmp_path):
    class BudgetObserver:
        budgets = []

        async def respond(self, *args, **kwargs):
            self.budgets.append(kwargs["budget_seconds"])
            return {"status": "completed", "message": "Tràng An có cảnh quan đẹp."}

    model = BudgetObserver()
    service = InformationService(configuration(tmp_path), model, transport=httpx.MockTransport(handler([])))
    await service.execute(
        "web.search", {"query": "Ninh Bình có gì du lịch?"}, event(), SessionState(), provider="google"
    )
    assert 5 < model.budgets[0] <= service.settings.information_timeout_seconds


def test_long_evidence_prompt_retains_relevant_fact_and_each_source():
    evidence = [
        source(f"s{i}", ("Nội dung giới thiệu chung. " * 400) + f" Hà Nội có {i + 1} triệu người.") for i in range(5)
    ]
    passages = synthesis_passages(evidence, "Dân số Hà Nội là bao nhiêu?")
    assert len(passages) <= 12 and sum(len(p["content"]) for p in passages.values()) <= 9600
    assert {p["source_id"] for p in passages.values()} == {f"s{i}" for i in range(5)}
    assert all(any(f"{i + 1} triệu người" in p["content"] for p in passages.values()) for i in range(5))


@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        ("898 triệu đến 1,079 tỷ đồng", "898.000.000 đến 1.079.000.000 đồng"),
        ("898 đến 1.079 triệu đồng", "898.000.000 đến 1.079.000.000 đồng"),
        ("1,1 tỷ đồng", "1.100 triệu đồng"),
        ("8 triệu người", "8.000.000 người"),
        ("898 triệu và 1,079 tỷ đồng", "Đơn vị tính: Triệu Đồng\n| Eco | 898 |\n| Plus | 1.079 |"),
    ],
)
def test_scaled_numeric_equivalence_is_exact(answer, evidence):
    assert equivalent_numeric_claims(answer) == equivalent_numeric_claims(evidence)


@pytest.mark.parametrize("invented", ["899 triệu đồng", "0,898 tỷ đến 1,080 tỷ đồng", "898 triệu đồng, năm 2027"])
@pytest.mark.asyncio
async def test_scaled_numbers_do_not_allow_invented_prices_or_dates(invented):
    client = ReferenceClient([{"text": invented, "evidence_ids": ["e1"]}] * 2)
    result = await composer(client).respond(
        "Giá xe?", provider="google", evidence=[source("a", "Giá 898.000.000 đồng và 1.079.000.000 đồng năm 2026.")]
    )
    assert result["error_code"] == "answer_not_grounded"
    assert result["generation_attempts"][0]["validation_error"] == "unsupported_numeric_claim"


@pytest.mark.asyncio
async def test_genuine_price_summary_with_scaled_units_does_not_repair_or_search_again(tmp_path):
    calls = []
    question = "Xe VinFast VF8 có giá bao nhiêu?"
    client = ReferenceClient(
        [{"text": "Giá niêm yết trong nguồn là 898 triệu đến 1,079 tỷ đồng.", "evidence_ids": ["e1"]}]
    )

    def respond(request):
        calls.append(request)
        assert "giá niêm yết xe mới tại Việt Nam" in json.loads(request.content)["query"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Bảng giá VF8",
                        "url": "https://example.org/vf8",
                        "content": "Giá niêm yết VF8: 898.000.000 và 1.079.000.000 đồng.",
                    }
                ]
            },
        )

    service = InformationService(configuration(tmp_path), composer(client), transport=httpx.MockTransport(respond))
    result = await service.execute(
        "web.search", {"query": question}, event(text=question), SessionState(), provider="google"
    )
    assert result["status"] == "completed" and len(calls) == len(client.calls) == 1
    assert json.loads(client.calls[0]["user"])["question"] == question
    assert result["information_context"]["search_query"] != question


@pytest.mark.parametrize(
    "query", ["Giá VF8 cũ", "Giá VF8 đã qua sử dụng", "Giá lăn bánh VF8 tại Hà Nội", "Giá VF8 tại Mỹ bằng USD"]
)
def test_explicit_price_scope_is_not_replaced_with_new_vietnam_retail(query):
    assert retrieval_query(query) == query
