"""Shared component fixtures extracted from historical scenario modules."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from src.vivi.config import Settings
from src.vivi.conversation.service import ConversationService
from src.vivi.dialogue.contracts import TurnEvent
from src.vivi.dialogue.planner import ModelProvider
from src.vivi.graphs import build_graph
from src.vivi.history.sqlite import SQLiteConversationHistory
from src.vivi.information.service import InformationService
from src.vivi.rag.generator import ExtractiveHandbookGenerator
from src.vivi.rag.runtime import HandbookServices


class NoRetrieval:
    def retrieve(self, *args):
        raise AssertionError("Open world must not retrieve handbook")


class AnswerClient:
    def __init__(self, invalid=False, failing=False):
        self.prompts = []
        self.invalid, self.failing = invalid, failing

    async def agenerate_json(self, **kwargs):
        self.prompts.append(json.loads(kwargs["user"]))
        if self.failing:
            raise RuntimeError("unavailable")
        data = self.prompts[-1]
        sources = data.get("evidence", [])
        return json.dumps(
            {
                "text": "Bạn có thể tham quan Tràng An để ngắm cảnh."
                if sources
                else "Ngày xưa có một chú mèo thích đi du lịch.",
                "supports": [
                    {
                        "source_id": "invented" if self.invalid else sources[0]["source_id"],
                        "quote": sources[0]["content"][:50],
                    }
                ]
                if sources
                else [],
            },
            ensure_ascii=False,
        )


def configuration(tmp_path, **kwargs):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        information_enabled=True,
        tavily_api_key="test-key",
        rag_local_only=False,
        jetson_ai_policy="disabled",
        **kwargs,
    )


def conversation(client=None):
    client = client or AnswerClient()
    return ConversationService({"fixture": ModelProvider("fixture", client, 1)}), client


def event(**kwargs):
    return TurnEvent(session_id="driver", turn_id="one", **kwargs)


def handler(calls):
    def respond(request):
        calls.append(request)
        if request.url.host == "api.tavily.com":
            assert request.headers["authorization"] == "Bearer test-key"
            body = json.loads(request.content)
            assert body["search_depth"] == "basic" and body["auto_parameters"] is False
            assert body["include_answer"] is False
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Tràng An",
                            "url": "https://example.org/trang-an",
                            "content": "Tràng An là địa điểm tham quan với cảnh quan núi đá và sông nước.",
                        }
                    ]
                },
            )
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Ninh Bình",
                            "country_code": "VN",
                            "latitude": 20.25,
                            "longitude": 105.97,
                            "timezone": "Asia/Ho_Chi_Minh",
                        }
                    ]
                },
            )
        if request.url.host == "api.open-meteo.com":
            today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
            return httpx.Response(
                200,
                json={
                    "timezone": "Asia/Ho_Chi_Minh",
                    "daily_units": {
                        "temperature_2m_min": "°C",
                        "temperature_2m_max": "°C",
                        "precipitation_probability_max": "%",
                    },
                    "daily": {
                        "time": [(today + timedelta(days=i)).isoformat() for i in range(3)],
                        "temperature_2m_min": [20, 21, 22],
                        "temperature_2m_max": [28, 29, 30],
                        "precipitation_probability_max": [10, 20, 30],
                    },
                },
            )
        if request.url.host.endswith("wikipedia.org"):
            assert request.url.params["prop"] == "extracts|info"
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "index": 1,
                                "title": "Tràng An",
                                "fullurl": "https://vi.wikipedia.org/wiki/Trang_An",
                                "extract": "Tràng An là một quần thể di sản.",
                            }
                        ]
                    }
                },
            )
        raise AssertionError(str(request.url))

    return respond


def graph_fixture(tmp_path):
    composer, _ = conversation()
    service = InformationService(configuration(tmp_path), composer, transport=httpx.MockTransport(handler([])))
    services = HandbookServices(
        NoRetrieval(),
        ExtractiveHandbookGenerator(),
        SQLiteConversationHistory(tmp_path / "history.db"),
        conversation=composer,
        information=service,
    )
    return build_graph(services)
