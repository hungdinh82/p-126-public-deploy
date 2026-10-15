"""Source references survive model paraphrasing without weakening validation."""

import json

import httpx
import pytest

from src.vivi.conversation.service import evidence_passages
from src.vivi.dialogue.contracts import SessionState
from src.vivi.information.service import InformationService
from tests.support.open_world import configuration, event, handler
from tests.support.source_synthesis import ReferenceClient as ReferenceClient
from tests.support.source_synthesis import composer as composer
from tests.support.source_synthesis import source as source


@pytest.mark.asyncio
async def test_tourism_synthesis_uses_server_owned_passages_and_original_citations():
    evidence = [
        source("travel-a", "Mỹ Khê có bãi biển đẹp; Bà Nà Hills có khu vui chơi."),
        source("travel-b", "Chợ đêm Sơn Trà có các gian hàng ẩm thực."),
    ]
    answer = {
        "text": "Bạn có thể thư giãn tại biển Mỹ Khê, chơi ở Bà Nà Hills hoặc thưởng thức ẩm thực tại chợ đêm Sơn Trà.",
        "evidence_ids": ["e1", "e2"],
    }
    client = ReferenceClient([answer])
    result = await composer(client).respond("Ở Đà Nẵng có những gì chơi?", provider="google", evidence=evidence)
    assert result["status"] == "completed" and result["message"] == answer["text"]
    assert result["grounding_status"] == "evidence_matched" and len(client.calls) == 1
    assert [item["source_id"] for item in result["citations"]] == ["travel-a", "travel-b"]
    assert all("content" not in item for item in result["citations"])
    schema = client.calls[0]["schema"]
    assert set(schema["properties"]) == {"text", "evidence_ids"}
    assert schema["properties"]["evidence_ids"]["items"]["enum"] == ["e1", "e2"]
    payload = json.loads(client.calls[0]["user"])
    assert payload["evidence"][1]["evidence_id"] == "e2"
    assert payload["evidence"][1]["content"] == evidence[1]["content"]


@pytest.mark.asyncio
async def test_unknown_reference_is_repaired_once_and_never_becomes_a_citation():
    client = ReferenceClient(
        [
            {"text": "Mỹ Khê có bãi biển đẹp.", "evidence_ids": ["e999"]},
            {"text": "Mỹ Khê có bãi biển đẹp.", "evidence_ids": ["e1", "e1"]},
        ]
    )
    result = await composer(client).respond(
        "Có gì chơi?", provider="google", evidence=[source("travel-a", "Mỹ Khê có bãi biển đẹp.")]
    )
    assert result["status"] == "completed" and len(client.calls) == 2
    assert len(result["citations"]) == 1
    assert result["generation_attempts"][0]["validation_error"] == "unknown_evidence_id"


@pytest.mark.parametrize("malformed", [None, [], "not an object"])
@pytest.mark.asyncio
async def test_non_object_model_json_gets_schema_repair(malformed):
    client = ReferenceClient([malformed, {"text": "Mỹ Khê có bãi biển đẹp.", "evidence_ids": ["e1"]}])
    result = await composer(client).respond(
        "Có gì chơi?", provider="google", evidence=[source("travel-a", "Mỹ Khê có bãi biển đẹp.")]
    )
    assert result["status"] == "completed" and len(client.calls) == 2
    assert result["generation_attempts"][0]["validation_error"] == "invalid_answer_schema"


@pytest.mark.parametrize(
    "answer",
    [
        {"text": "Vé Mỹ Khê giá 999.000 đồng.", "evidence_ids": ["e1"]},
        {"text": "Mình đã mở cửa xe cho bạn.", "evidence_ids": ["e1"]},
        {"text": "Mỹ Khê có bãi biển đẹp.", "evidence_ids": ["e404"]},
    ],
)
@pytest.mark.asyncio
async def test_invalid_source_answer_is_blocked_even_with_reference_contract(answer):
    client = ReferenceClient([answer, answer])
    result = await composer(client).respond(
        "Có gì chơi?", provider="google", evidence=[source("travel-a", "Mỹ Khê có bãi biển đẹp.")]
    )
    assert result["status"] == "blocked" and result["error_code"] == "answer_not_grounded"
    assert len(client.calls) == 2


def test_passages_preserve_exact_source_text_and_bound_quote_size():
    evidence = [source("long", "Khung cảnh núi rừng. " * 90 + "Giá vé TEST là 20.000 đồng.")]
    passages = evidence_passages(evidence)
    assert len(passages) > 1
    assert all(len(p["content"]) <= 800 and p["content"] in evidence[0]["content"] for p in passages.values())
    assert "Giá vé TEST là 20.000 đồng." in list(passages.values())[-1]["content"]


@pytest.mark.asyncio
async def test_numeric_claim_must_be_in_selected_passage_not_another_part_of_source():
    evidence = [source("long", "Mỹ Khê có bãi biển đẹp. " * 70 + "Giá vé TEST là 20.000 đồng.")]
    answer = {"text": "Giá vé TEST là 20.000 đồng.", "evidence_ids": ["e1"]}
    result = await composer(ReferenceClient([answer, answer])).respond("Giá vé?", provider="google", evidence=evidence)
    assert result["status"] == "blocked"
    assert result["generation_attempts"][0]["validation_error"] == "unsupported_numeric_claim"


@pytest.mark.asyncio
async def test_information_adapter_returns_summary_and_cached_sources_not_only_links(tmp_path):
    client = ReferenceClient(
        [{"text": "Bạn có thể tham quan Tràng An để ngắm cảnh núi đá và sông nước.", "evidence_ids": ["e1"]}] * 2
    )
    calls = []
    service = InformationService(
        configuration(tmp_path), composer(client), transport=httpx.MockTransport(handler(calls))
    )
    first = await service.execute(
        "web.search", {"query": "Có gì chơi ở Ninh Bình?"}, event(), SessionState(), provider="google"
    )
    second = await service.execute(
        "web.search", {"query": "Có gì chơi ở Ninh Bình?"}, event(), SessionState(), provider="google"
    )
    assert first["status"] == second["status"] == "completed"
    assert "Tràng An" in first["message"] and "http" not in first["message"]
    assert first["citations"] and first["source_count"] == 1
    assert second["cache_hit"] and len(calls) == 1
