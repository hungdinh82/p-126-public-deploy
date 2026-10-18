"""Synthetic evidence through real information/grounding services; no sockets."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from src.vivi.config import Settings
from src.vivi.conversation.service import ConversationService
from src.vivi.information.service import InformationService
from src.vivi.model_gateway import ModelProvider
from src.vivi.rag.schemas import RetrievedChunk

CONTENT = "Tràng An là địa điểm tham quan với núi đá và sông nước."


class EvidenceAnswer:
    async def agenerate_json(self, **kwargs):
        data = json.loads(kwargs["user"])
        evidence = data.get("evidence", [])
        if evidence:
            return json.dumps({"text": evidence[0]["content"], "evidence_ids": [evidence[0]["evidence_id"]]}, ensure_ascii=False)
        return json.dumps({"text": "Ngày xưa có một chú mèo thích đi du lịch."}, ensure_ascii=False)


class HandbookEvidence:
    def retrieve(self, *_args):
        return [RetrievedChunk(
            source_id="benchmark-vf8-door", document_id="benchmark-manual",
            source_url="https://example.org/benchmark/manual", vehicle_model="VF8",
            model_year=2026, locale="vi_vn", chapter_id=1, chapter="Cửa xe",
            section_path=["Cửa xe"], content_type="paragraph",
            content="Cẩm nang benchmark VF8: đỗ xe trước khi mở cửa.",
            checksum="synthetic-v1", chunk_index=0,
        )]


def respond(request):
    host = request.url.host
    if host == "api.tavily.com":
        return httpx.Response(200, json={"results": [{"title": "Benchmark Tràng An",
            "url": "https://example.org/benchmark/trang-an", "content": CONTENT}]})
    if host.endswith("wikipedia.org"):
        return httpx.Response(200, json={"query": {"pages": [{"index": 1, "title": "Tràng An",
            "fullurl": "https://vi.wikipedia.org/wiki/Trang_An", "extract": CONTENT}]}})
    if host == "geocoding-api.open-meteo.com":
        return httpx.Response(200, json={"results": [{"name": "Hà Nội", "country_code": "VN",
            "latitude": 21.02, "longitude": 105.84, "timezone": "Asia/Ho_Chi_Minh", "feature_code": "PPLC"}]})
    if host == "api.open-meteo.com":
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
        return httpx.Response(200, json={"timezone": "Asia/Ho_Chi_Minh", "daily_units": {
            "temperature_2m_min": "°C", "temperature_2m_max": "°C", "precipitation_probability_max": "%"},
            "daily": {"time": [(today + timedelta(days=i)).isoformat() for i in range(3)],
                      "temperature_2m_min": [20, 21, 22], "temperature_2m_max": [28, 29, 30],
                      "precipitation_probability_max": [10, 20, 30]}})
    raise AssertionError(f"Unexpected benchmark HTTP dependency: {host}")


def attach(services, root, provider):
    conversation = ConversationService({provider: ModelProvider(provider, EvidenceAnswer(), 2)},
                                       fallback_chains={provider: [provider]})
    # model_copy ensures these fixture settings cannot inherit offline/env flags.
    config = Settings(_env_file=None, data_dir=root).model_copy(update={
        "information_enabled": True, "tavily_api_key": "benchmark-fake-key",
        "rag_local_only": False, "jetson_ai_policy": "disabled",
    })
    services.conversation = conversation
    services.information = InformationService(config, conversation, transport=httpx.MockTransport(respond))
    services.retriever = HandbookEvidence()
