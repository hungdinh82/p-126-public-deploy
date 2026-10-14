"""Replay Oct 14 information failures using synthetic facts and mock HTTP."""

import json

import httpx
import pytest

from src.vivi.dialogue.answers import AnswerResolver
from src.vivi.dialogue.contracts import DialogueTask, QuestionReply, SemanticTurn, SessionState, TaskStep
from src.vivi.dialogue.parser import OfflineParser
from src.vivi.dialogue.questions import current_question
from src.vivi.dialogue.reducer import synchronize_question
from src.vivi.information.policy import encyclopedia_topic, information_decision
from src.vivi.information.service import InformationService
from tests.test_conversation_generalization import pending
from tests.test_open_world import configuration, conversation, event, handler
from tests.test_source_synthesis import ReferenceClient, composer, source


@pytest.mark.parametrize("question", [
    "Ai đang là hiệu trưởng của Trường Aurora?", "Ai đang là tổng bí thư ở Việt Nam?",
    "Ai là CEO của Aurora?", "Việt Nam có bao nhiêu tỉnh thành?", "Hà Nội rộng bao nhiêu km2?",
    "Dân số Hà Nội là bao nhiêu?", "Giá xăng hôm nay", "Giờ mở cửa của bảo tàng?",
])
def test_changing_facts_cannot_be_downgraded_to_model_or_wikipedia(question):
    parser = OfflineParser()
    for capability in ["conversation.respond", "knowledge.lookup"]:
        draft = SemanticTurn(act="question", actions=[{"capability": capability, "arguments": {"query": question}}])
        guarded = parser.guard(question, draft, SessionState())
        assert guarded.actions[0].capability == "web.search"
        assert guarded.actions[0].arguments.query == question


def test_stable_explanation_can_use_model_and_model_can_choose_citations():
    question = "Vì sao có cầu vồng?"
    parser = OfflineParser()
    assert information_decision(question) is None
    assert parser.interpret(question, SessionState()).actions[0].capability == "conversation.respond"
    for capability in ["knowledge.lookup", "web.search"]:
        draft = SemanticTurn(act="question", actions=[{"capability": capability, "arguments": {"query": question}}])
        assert parser.guard(question, draft, SessionState()) == draft


@pytest.mark.parametrize("question", ["Hôm nay là ngày bao nhiêu?", "Năm nay là năm bao nhiêu?"])
@pytest.mark.asyncio
async def test_runtime_clock_bypasses_model_and_cannot_be_swallowed_as_place(question):
    state = pending()
    state.navigation_workspace.draft.clarification.update(phase="specific", choices=[])
    parsed = OfflineParser().interpret(question, state)
    assert parsed.act == "question" and parsed.actions[0].capability == "conversation.respond"
    client = ReferenceClient([])
    result = await composer(client).respond(question, provider="google")
    assert result["answer_basis"] == "runtime" and result["grounding_status"] == "supported"
    assert not client.calls


@pytest.mark.asyncio
async def test_unqualified_weather_defaults_to_hanoi_even_when_gps_is_elsewhere(tmp_path):
    calls = []
    fallback = handler(calls)

    def respond(request):
        if request.url.host == "geocoding-api.open-meteo.com":
            calls.append(request)
            name = request.url.params["name"]
            assert name in {"Hà Nội", "Đà Nẵng"}
            return httpx.Response(200, json={"results": [{"name": name, "country_code": "VN",
                "latitude": 21.02, "longitude": 105.84, "feature_code": "PPLC"}]})
        return fallback(request)

    service = InformationService(configuration(tmp_path), conversation()[0], transport=httpx.MockTransport(respond))
    result = await service.execute("weather.get_forecast", {"query": "Thời tiết hôm nay"},
        event(position={"lat": 16, "lng": 108}), SessionState(), provider="rules")
    assert result["status"] == "completed" and result["data"]["location"] == "Hà Nội"
    assert calls[-1].url.params["latitude"] == "21.02"
    assert result["message"].startswith("Ở Hà Nội hôm nay") and result["data"]["date"]
    service.settings.weather_default_location = "Đà Nẵng"
    changed = await service.execute("weather.get_forecast", {"query": "Thời tiết hôm nay"},
        event(position={"lat": 16, "lng": 108}), SessionState(), provider="rules")
    assert changed["data"]["location"] == "Đà Nẵng" and not changed["cache_hit"]
    assert sum(r.url.host == "geocoding-api.open-meteo.com" for r in calls) == 2


@pytest.mark.parametrize("island", [False, True])
@pytest.mark.asyncio
async def test_phu_quoc_uses_metadata_without_three_identical_choices(tmp_path, island):
    calls = []
    fallback = handler(calls)

    def respond(request):
        if request.url.host == "geocoding-api.open-meteo.com":
            calls.append(request)
            assert request.url.params["name"] == "Phú Quốc"
            places = [{"name": "Phú Quốc", "country_code": "VN", "country": "Việt Nam", "admin1": "An Giang",
                "latitude": 10 + i * .03, "longitude": 104, "feature_code": code}
                for i, code in enumerate(["PPL", "PPLA2", "ISL"])]
            return httpx.Response(200, json={"results": places})
        return fallback(request)

    location = "đảo Phú Quốc" if island else "Phú Quốc"
    service = InformationService(configuration(tmp_path), conversation()[0], transport=httpx.MockTransport(respond))
    result = await service.execute("weather.get_forecast", {"query": "Thời tiết " + location, "location": location},
                                  event(), SessionState(), provider="rules")
    assert result["status"] == "completed"
    assert float(calls[-1].url.params["latitude"]) == (10.06 if island else 10.03)


@pytest.mark.parametrize("reply", ["Cái đầu tiên.", "Chọn chỗ thứ nhất", "Thứ hai"])
def test_weather_choices_use_shared_question_contract_and_keep_task(reply):
    state = SessionState(active=DialogueTask(steps=[TaskStep(draft={"capability": "weather.get_forecast",
        "arguments": {"query": "Thời tiết ngày mai", "forecast_day": "tomorrow"}}, status="waiting_slot")]),
        conversation_context={"capability": "weather.get_forecast", "location_candidates": [
            {"label": "Thành phố A, Tỉnh A", "lat": 21, "lng": 105},
            {"label": "Thành phố A, Tỉnh B", "lat": 12, "lng": 109},
        ]})
    state = synchronize_question(state)
    q = current_question(state)
    index = 1 if reply == "Thứ hai" else 0
    voice = OfflineParser().interpret(reply, state)
    typed = AnswerResolver().resolve("", state, reply=QuestionReply(question_id=q.id, option_id=q.options[index].id))
    assert voice == typed and voice.act == "modify"
    assert voice.actions[0].arguments.location == q.options[index].label
    assert voice.actions[0].arguments.forecast_day == "tomorrow"


@pytest.mark.asyncio
async def test_current_question_keeps_who_when_model_retrieval_query_lost_it(tmp_path):
    question = "Ai đang là hiệu trưởng của Trường Aurora?"
    client = ReferenceClient([{"text": "Hiệu trưởng Trường Aurora hiện là bà Nguyễn Minh An.", "evidence_ids": ["e1"]}])
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "api.tavily.com"
        assert json.loads(request.content)["query"] == question
        return httpx.Response(200, json={"results": [{"title": "Lãnh đạo Aurora", "url": "https://aurora.example.org/leadership",
            "content": "Hiệu trưởng Trường Aurora hiện là bà Nguyễn Minh An."}]})

    service = InformationService(configuration(tmp_path), composer(client), transport=httpx.MockTransport(respond))
    result = await service.execute("knowledge.lookup", {"query": "hiệu trưởng Trường Aurora"},
                                   event(text=question), SessionState(), provider="google")
    assert result["status"] == "completed" and result["information_provider"] == "tavily"
    assert json.loads(client.calls[0]["user"])["question"] == question
    assert result["source_policy"] == "current_office_holder" and len(calls) == 1


@pytest.mark.asyncio
async def test_definition_with_a_citation_still_does_not_answer_who():
    client = ReferenceClient([{"text": "Hiệu trưởng là một chức danh lãnh đạo trong trường học.", "evidence_ids": ["e1"]}] * 2)
    result = await composer(client).respond("Ai đang là hiệu trưởng Aurora?", provider="google",
        evidence=[source("s1", "Hiệu trưởng là một chức danh lãnh đạo trong trường học.")])
    assert result["status"] == "blocked" and result["error_code"] == "answer_not_grounded"
    assert result["generation_attempts"][0]["validation_error"] == "answer_does_not_address_question"


@pytest.mark.asyncio
async def test_one_richer_search_can_supply_missing_fact_and_is_cached(tmp_path):
    calls = []

    class Client:
        async def agenerate_json(self, **kwargs):
            evidence = json.loads(kwargs["user"])["evidence"]
            found = next((p for p in evidence if "Nguyễn Minh An" in p["content"]), None)
            return json.dumps({"text": "CEO Aurora hiện là bà Nguyễn Minh An." if found else "Chưa đủ thông tin.",
                               "evidence_ids": [found["evidence_id"]] if found else []}, ensure_ascii=False)

    def respond(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["search_depth"] == "basic" and payload["auto_parameters"] is False
        return httpx.Response(200, json={"results": [{"title": "Aurora", "url": "https://aurora.example.org/team",
            "content": "Aurora là công ty phần mềm.", "raw_content": "CEO Aurora hiện là bà Nguyễn Minh An."
            if payload["include_raw_content"] == "text" else None}]})

    service = InformationService(configuration(tmp_path), composer(Client()), transport=httpx.MockTransport(respond))
    for _ in range(2):
        result = await service.execute("web.search", {"query": "Ai là CEO Aurora?"}, event(), SessionState(), provider="google")
        assert result["status"] == "completed" and "Nguyễn Minh An" in result["message"]
        assert result["citations"][0]["source_url"] == "https://aurora.example.org/team"
    assert len(calls) == 2 and result["cache_hit"]


@pytest.mark.asyncio
async def test_insufficient_wikipedia_escalates_once_without_model_knowledge(tmp_path):
    calls = []
    client = ReferenceClient([{"text": "Chưa có dữ liệu.", "evidence_ids": []},
        {"text": "Aurora được thành lập năm 1800.", "evidence_ids": ["e1"]}])

    def respond(request):
        calls.append(request)
        if request.url.host.endswith("wikipedia.org"):
            assert request.url.params["gsrsearch"] == "Aurora"
            assert "exintro" not in request.url.params
            return httpx.Response(200, json={"query": {"pages": [{"title": "Aurora", "index": 1,
                "fullurl": "https://vi.wikipedia.org/wiki/Aurora", "extract": "Aurora là một thành phố."}]}})
        return httpx.Response(200, json={"results": [{"title": "Lịch sử Aurora", "url": "https://aurora.example.org/history",
            "content": "Aurora được thành lập năm 1800."}]})

    service = InformationService(configuration(tmp_path), composer(client), transport=httpx.MockTransport(respond))
    result = await service.execute("knowledge.lookup", {"query": "Lịch sử Aurora"}, event(), SessionState(), provider="google")
    assert result["status"] == "completed" and result["information_provider"] == "tavily"
    assert result["answer_basis"] == "sources" and len(calls) == 2 and len(result["retrieval_attempts"]) == 2
    assert encyclopedia_topic("Hà Nội rộng bao nhiêu km2?") == "Hà Nội"


@pytest.mark.asyncio
async def test_forecast_routing_needs_no_planner_model():
    from src.vivi.dialogue.planner import ModelProvider, SemanticResolver

    client = ReferenceClient([])
    resolver = SemanticResolver({"google": ModelProvider("google", client, 1)})
    result, provider, attempts = await resolver.resolve("Thời tiết Phú Quốc hôm nay như nào?", SessionState(), requested="google")
    assert result.actions[0].capability == "weather.get_forecast" and provider == "rules"
    assert attempts[0]["source"] == "information_policy" and not client.calls


@pytest.mark.asyncio
async def test_quantity_question_cannot_be_answered_by_related_generic_text():
    client = ReferenceClient([{"text": "Aurora là một thành phố phát triển.", "evidence_ids": ["e1"]}] * 2)
    result = await composer(client).respond("Aurora có bao nhiêu cư dân?", provider="google",
        evidence=[source("s1", "Aurora là một thành phố phát triển.")])
    assert result["error_code"] == "answer_not_grounded"
    assert result["generation_attempts"][0]["validation_error"] == "answer_does_not_address_question"


@pytest.mark.asyncio
async def test_mixed_turn_only_sends_query_for_information_step(tmp_path):
    query = "Ai là CEO Aurora?"
    state = SessionState(active=DialogueTask(steps=[
        TaskStep(draft={"capability": "memory.remember", "arguments": {"memory_key": "note.secret", "memory_value": "PRIVATE"}}, status="completed"),
        TaskStep(draft={"capability": "web.search", "arguments": {"query": query}}),
    ]))
    client = ReferenceClient([{"text": "CEO Aurora hiện là bà Nguyễn Minh An.", "evidence_ids": ["e1"]}])

    def respond(request):
        assert json.loads(request.content)["query"] == query and "PRIVATE" not in request.content.decode()
        return httpx.Response(200, json={"results": [{"title": "Aurora", "url": "https://aurora.example.org/team",
            "content": "CEO Aurora hiện là bà Nguyễn Minh An."}]})

    service = InformationService(configuration(tmp_path), composer(client), transport=httpx.MockTransport(respond))
    result = await service.execute("web.search", {"query": query}, event(text="Nhớ PRIVATE rồi cho biết " + query), state, provider="google")
    assert result["status"] == "completed" and "PRIVATE" not in client.calls[0]["user"]
