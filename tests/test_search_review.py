"""Search review: relative dates, topic follow-ups, failures and provider fallback."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from src.vivi.conversation.service import ConversationService, numeric_claims
from src.vivi.dialogue.contracts import SemanticTurn, SessionState
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.dialogue.planner import ModelProvider
from src.vivi.information.service import InformationService
from src.vivi.information.time_context import search_time_context
from tests.test_open_world import configuration, event, handler
from tests.test_source_synthesis import ReferenceClient, composer, source


def tourism_session():
    return SessionState(
        conversation_context={
            "capability": "web.search",
            "query": "Ở Ninh Bình có gì chơi?",
            "focus": "information",
        }
    )


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Hôm nay là ngày bao nhiêu âm lịch?", "ngày 13/10/2026"),
        ("Giá vàng hôm nay", "Giá vàng ngày 13/10/2026"),
        ("Sự kiện cuối tháng này ở Hà Nội", "cuối tháng 10/2026"),
        ("Sự kiện tháng tới", "tháng 11/2026"),
        ("Sự kiện cuối tháng tới", "cuối tháng 11/2026"),
        ("Sự kiện cuối tuần này", "17/10/2026 đến 18/10/2026"),
        ("Tin mới tuần này", "12/10/2026 đến 18/10/2026"),
        ("Ngày mai ở Hà Nội có gì chơi?", "ngày 14/10/2026"),
        ("Ngày kia ở Hà Nội có gì chơi?", "ngày 15/10/2026"),
        ("Du lịch năm nay", "năm 2026"),
        ("Ở Ninh Bình có gì chơi?", "Ở Ninh Bình có gì chơi?"),
        ("Sự kiện tháng 10/2025", "Sự kiện tháng 10/2025"),
    ],
)
def test_search_dates_are_local_absolute_and_preserve_explicit_history(query, expected):
    context = search_time_context(query, "Asia/Ho_Chi_Minh", now=datetime(2026, 10, 12, 18, tzinfo=UTC))
    assert context["current_date"] == "2026-10-13"
    assert expected in context["search_query"]
    assert context["timezone"] == "Asia/Ho_Chi_Minh"


def test_month_and_year_rollover_and_non_vietnam_timezone():
    now = datetime(2026, 12, 31, 18, tzinfo=UTC)
    local = search_time_context("Tháng tới và ngày mai", "Asia/Ho_Chi_Minh", now=now)
    assert local["search_query"] == "tháng 2/2027 và ngày 02/01/2027"
    utc = search_time_context("Tháng tới và ngày mai", "UTC", now=now)
    assert utc["search_query"] == "tháng 1/2027 và ngày 01/01/2027"


@pytest.mark.parametrize(
    "query",
    [
        "Có chỗ nào phù hợp trẻ em không?",
        "Còn buổi tối thì sao?",
        "Thế còn ăn uống?",
        "Ở đó có chỗ nào ăn ngon?",
        "Còn địa điểm nào khác?",
    ],
)
def test_search_followups_keep_preceding_topic_and_override_missing_model_context(query):
    session = tourism_session()
    # An unaccepted navigation target must not replace the search's topic.
    session.navigation_workspace.draft = {"selected": {"name": "Đà Nẵng"}}
    parser = OfflineParser()
    result = parser.interpret(query, session)
    assert result.actions[0].capability == "web.search"
    assert "Ninh Bình" in result.actions[0].arguments.query and "Đà Nẵng" not in result.actions[0].arguments.query
    semantic = SemanticTurn(act="question", actions=[{"capability": "web.search", "arguments": {"query": query}}])
    assert parser.guard(query, semantic, session).actions[0].arguments.query == result.actions[0].arguments.query


@pytest.mark.parametrize(
    "query,capability",
    [
        ("Tìm quán cà phê gần nhất", "navigation.search"),
        ("Dẫn đường đến Tràng An", "navigation.plan"),
        ("Pin xe còn bao nhiêu?", "vehicle.get_status"),
        ("Thời tiết Hà Nội ngày mai", "weather.get_forecast"),
        ("Ở Hải Phòng có gì chơi?", "web.search"),
        ("Vì sao có cầu vồng?", "conversation.respond"),
        ("Hôm nay là bao nhiêu âm?", "web.search"),
        ("Sự kiện ở Hà Nội cuối tháng này", "web.search"),
    ],
)
def test_search_followup_does_not_absorb_new_topics_commands_or_weather(query, capability):
    result = OfflineParser().interpret(query, tourism_session())
    assert result.actions[0].capability == capability
    assert "chủ đề:" not in (result.actions[0].arguments.query or "")


@pytest.mark.asyncio
async def test_two_followups_reuse_original_topic_and_new_city_replaces_it(tmp_path):
    model = ReferenceClient([{"text": "Tràng An có cảnh núi đá và sông nước.", "evidence_ids": ["e1"]}] * 4)
    calls = []
    service = InformationService(
        configuration(tmp_path), composer(model), transport=httpx.MockTransport(handler(calls))
    )
    session = tourism_session()
    parser = OfflineParser()
    for query in [
        "Còn buổi tối thì sao?",
        "Có chỗ nào phù hợp trẻ em không?",
        "Ở Hải Phòng có gì chơi?",
        "Còn buổi tối thì sao?",
    ]:
        action = parser.interpret(query, session).actions[0]
        result = await service.execute(
            action.capability, action.arguments.model_dump(exclude_none=True), event(), session, provider="google"
        )
        assert result["status"] == "completed"
        session.conversation_context = {**result["information_context"], "focus": "information"}
    queries = [json.loads(call.content)["query"] for call in calls]
    assert "Ninh Bình" in queries[0] and "Ninh Bình" in queries[1]
    assert queries[1].count("chủ đề:") == 1
    assert "Ninh Bình" not in queries[2] and "Ninh Bình" not in queries[3]
    assert "Hải Phòng" in queries[3]


@pytest.mark.asyncio
async def test_search_and_synthesis_share_date_context_without_sending_memory(tmp_path, monkeypatch):
    import src.vivi.information.service as module

    original = search_time_context
    monkeypatch.setattr(
        module,
        "search_time_context",
        lambda query, timezone: original(query, timezone, now=datetime(2026, 10, 12, 18, tzinfo=UTC)),
    )
    calls = []
    model = ReferenceClient(
        [{"text": "Mình chưa xác minh được sự kiện cuối tháng này từ nguồn đã tìm.", "evidence_ids": ["e1"]}]
    )
    service = InformationService(
        configuration(tmp_path), composer(model), transport=httpx.MockTransport(handler(calls))
    )
    result = await service.execute(
        "web.search",
        {"query": "Sự kiện ở Hà Nội cuối tháng này"},
        event(),
        SessionState(),
        provider="google",
        memory=[{"key": "note.secret", "value": "PRIVATE"}],
    )
    assert result["status"] == "completed"
    wire = json.loads(calls[0].content)
    assert "tháng 10/2026" in wire["query"] and "PRIVATE" not in calls[0].content.decode()
    prompt = json.loads(model.calls[0]["user"])
    assert prompt["context"]["current_date"] == "2026-10-13"
    assert prompt["context"]["search_query"] == wire["query"]
    assert "publication date alone" in model.calls[0]["system"]


@pytest.mark.asyncio
async def test_deadline_during_synthesis_keeps_sources_and_cache_for_retry(tmp_path):
    started = asyncio.Event()

    class SlowClient:
        async def agenerate_json(self, **kwargs):
            started.set()
            await asyncio.Future()

    model = ConversationService({"google": ModelProvider("google", SlowClient(), 5)})
    calls = []
    service = InformationService(
        configuration(tmp_path, information_timeout_seconds=1), model, transport=httpx.MockTransport(handler(calls))
    )
    first = await service.execute(
        "web.search", {"query": "Ninh Bình có gì chơi?"}, event(), SessionState(), provider="google"
    )
    assert started.is_set()
    assert first["error_code"] == "information_timeout" and first["answer_basis"] == "sources"
    assert first["citations"][0]["source_url"] == "https://example.org/trang-an"
    assert "content" not in first["citations"][0] and first["source_count"] == 1
    service.conversation = composer(
        ReferenceClient([{"text": "Tràng An có cảnh núi đá và sông nước.", "evidence_ids": ["e1"]}])
    )
    second = await service.execute(
        "web.search", {"query": "Ninh Bình có gì chơi?"}, event(), SessionState(), provider="google"
    )
    assert second["status"] == "completed" and second["cache_hit"] and len(calls) == 1


@pytest.mark.parametrize("payload", [None, [], {"results": None}, {"results": {}}])
@pytest.mark.asyncio
async def test_malformed_search_payload_returns_typed_failure(tmp_path, payload):
    model = ReferenceClient([])
    service = InformationService(
        configuration(tmp_path),
        composer(model),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    )
    result = await service.execute(
        "web.search", {"query": "Ninh Bình có gì chơi?"}, event(), SessionState(), provider="google"
    )
    assert result["status"] == "blocked" and result["error_code"] in {
        "search_response_invalid",
        "information_unavailable",
    }
    assert not model.calls


@pytest.mark.asyncio
async def test_empty_search_does_not_call_generator(tmp_path):
    model = ReferenceClient([])
    service = InformationService(
        configuration(tmp_path),
        composer(model),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})),
    )
    result = await service.execute(
        "web.search", {"query": "Ninh Bình có gì chơi?"}, event(), SessionState(), provider="google"
    )
    assert result["error_code"] == "no_evidence" and not model.calls


@pytest.mark.asyncio
async def test_generator_falls_back_to_local_using_same_sources(tmp_path):
    class BrokenCloud:
        async def agenerate_json(self, **kwargs):
            raise httpx.ConnectError("test unavailable")

    local = ReferenceClient([{"text": "Tràng An có cảnh núi đá và sông nước.", "evidence_ids": ["e1"]}])
    model = ConversationService(
        {"google": ModelProvider("google", BrokenCloud(), 1), "local": ModelProvider("local", local, 1)}
    )
    calls = []
    service = InformationService(configuration(tmp_path), model, transport=httpx.MockTransport(handler(calls)))
    result = await service.execute(
        "web.search", {"query": "Ninh Bình có gì chơi?"}, event(), SessionState(), provider="google"
    )
    assert result["status"] == "completed" and result["generation_provider"] == "local"
    assert len(calls) == 1 and result["citations"][0]["source_url"] == "https://example.org/trang-an"


@pytest.mark.parametrize(
    "answer",
    [
        "Festival dự kiến diễn ra từ ngày 16 đến 18 tháng 10 năm 2026.",
        "Festival dự kiến diễn ra từ ngày 16 đến 18-10-2026.",
        "Festival dự kiến diễn ra từ ngày 16 đến 18/10/2026.",
    ],
)
@pytest.mark.asyncio
async def test_news_date_formats_do_not_require_quote_retyping_or_numeric_repair(answer):
    client = ReferenceClient([{"text": answer, "evidence_ids": ["e1"]}])
    result = await composer(client).respond(
        "Festival diễn ra khi nào?",
        provider="google",
        evidence=[source("news", "Festival Thu Hà Nội 2026 dự kiến diễn ra từ ngày 16 đến 18.10.")],
    )
    assert result["status"] == "completed" and len(client.calls) == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ngày 18.10", {"18", "10"}),
        ("ngày 16-18.10", {"16", "18", "10"}),
        ("ngày 18.10.2026", {"18", "10", "2026"}),
        ("ngày 01.10.2026", {"1", "10", "2026"}),
        ("giá 18.10 triệu", {"1810"}),
        ("giá vé từ 18.10 triệu", {"1810"}),
        ("ngày 1 có giá vé từ 18.10 triệu", {"1", "1810"}),
        ("giá vé 999.000 đồng", {"999000"}),
        ("giá 150,5 triệu", {"1505"}),
        ("99.99", {"9999"}),
    ],
)
def test_date_numeric_normalization_does_not_split_prices(text, expected):
    assert numeric_claims(text) == expected


@pytest.mark.parametrize("text", ["Giá vé 1810 đồng.", "Festival diễn ra vào ngày 19 tháng 10."])
@pytest.mark.asyncio
async def test_date_normalization_still_blocks_invented_prices_and_dates(text):
    client = ReferenceClient([{"text": text, "evidence_ids": ["e1"]}] * 2)
    result = await composer(client).respond(
        "Festival diễn ra khi nào?",
        provider="google",
        evidence=[source("news", "Festival dự kiến diễn ra ngày 18.10.")],
    )
    assert result["status"] == "blocked"
    assert result["generation_attempts"][0]["validation_error"] == "unsupported_numeric_claim"


@pytest.mark.asyncio
async def test_empty_evidence_ids_abstain_once_and_discard_unsupported_model_text():
    client = ReferenceClient([{"text": "Giá vé là 999.000 đồng và mình đã mở cửa xe.", "evidence_ids": []}])
    result = await composer(client).respond(
        "Có sự kiện gì?", provider="google", evidence=[source("news", "Chưa công bố lịch sự kiện.")]
    )
    assert len(client.calls) == 1 and result["error_code"] == "insufficient_evidence"
    assert "999" not in result["message"] and "mở cửa" not in result["message"]
    assert result["citations"][0]["source_url"] == "https://example.org/news"
    attempt = result["generation_attempts"][0]
    assert (attempt["provider"], attempt["outcome"], attempt["location"]) == (
        "google", "insufficient_evidence", "cloud"
    )
    assert attempt["requested_model"] is None and attempt["served_model"] is None


@pytest.mark.parametrize(
    "url,date_suffix,expected",
    [
        ("https://doji.vn/quote", "0", "current_quote_not_verified"),
        ("https://doji.vn.evil.example/quote", "", "no_evidence"),
        ("https://fake-doji.vn/quote", "", "no_evidence"),
    ],
)
@pytest.mark.asyncio
async def test_gold_checks_date_boundaries_and_source_domains(tmp_path, url, date_suffix, expected):
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
    client = ReferenceClient([])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "results": [
                    {"url": url, "title": "Giá vàng TEST", "content": "Ngày " + today + date_suffix + ": báo giá TEST."}
                ]
            },
        )
    )
    service = InformationService(configuration(tmp_path), composer(client), transport=transport)
    result = await service.execute(
        "web.search", {"query": "Giá vàng hôm nay"}, event(), SessionState(), provider="google"
    )
    assert result["error_code"] == expected and not client.calls
    assert result["information_provider"] == "tavily" and result["source_count"] in {0, 1}
