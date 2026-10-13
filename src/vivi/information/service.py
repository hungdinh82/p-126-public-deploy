"""Three bounded HTTP adapters; no vehicle tools or arbitrary page fetching."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from src.vivi.conversation.context import destination, resolve_query, search_topic
from src.vivi.information.time_context import search_time_context
from src.vivi.text import normalize_text


class InformationError(Exception):
    def __init__(self, code, message):
        super().__init__(code)
        self.code, self.message = code, message


def public_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
            return None
        if host == "localhost" or host.endswith((".local", ".internal")) or "." not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return value
    except (ValueError, TypeError):
        return None


def plain(value):
    import html

    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def source_identifier(kind, *values):
    digest = hashlib.sha256("\0".join(str(value) for value in values).encode()).hexdigest()[:16]
    return kind + "_" + digest


class TavilyQuota:
    """Atomic reservation persists across restarts; contains no queries or keys."""

    def __init__(self, path, limit):
        self.path, self.limit = Path(path), limit

    def reserve(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        month = datetime.now(UTC).strftime("%Y-%m")
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("CREATE TABLE IF NOT EXISTS search_usage(month TEXT PRIMARY KEY, calls INTEGER NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO search_usage VALUES (?, 0)", (month,))
            used = db.execute("SELECT calls FROM search_usage WHERE month=?", (month,)).fetchone()[0]
            if used >= self.limit:
                return False
            db.execute("UPDATE search_usage SET calls=calls+1 WHERE month=?", (month,))
        return True


class InformationService:
    def __init__(self, settings, conversation, *, transport=None):
        self.settings, self.conversation, self.transport = settings, conversation, transport
        self.quota = TavilyQuota(settings.data_dir / "runtime" / "information-usage.sqlite3", settings.tavily_monthly_limit)
        self.cache = {}
        self.search_unavailable_until = 0

    @property
    def online(self):
        return self.settings.information_enabled and self.settings.jetson_ai_policy != "offline_only" and not self.settings.rag_local_only

    async def request(self, method, url, **kwargs):
        # Enforce policy immediately before each outbound HTTP operation.
        if not self.online:
            raise InformationError("internet_disabled", "Mình đang không truy cập internet nên chưa lấy được thông tin mới.")
        async with httpx.AsyncClient(timeout=self.settings.information_http_timeout_seconds, transport=self.transport,
                                     follow_redirects=False, headers={"User-Agent": "ViVi-CabinCopilot/1.0"}) as client:
            async with client.stream(method, url, **kwargs) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise InformationError("response_too_large", "Nguồn trả về quá nhiều dữ liệu; bạn thử câu hỏi cụ thể hơn nhé.")
                return json.loads(body)

    async def search(self, query):
        if not self.settings.tavily_api_key:
            raise InformationError("search_not_configured", "Mình chưa kết nối dịch vụ tìm kiếm internet.")
        if time.monotonic() < self.search_unavailable_until:
            raise InformationError("search_unavailable", "Dịch vụ tìm kiếm đang tạm không khả dụng. Bạn thử lại sau nhé.")
        if not await asyncio.to_thread(self.quota.reserve):
            raise InformationError("search_quota_exhausted", "Hạn mức tìm kiếm miễn phí tháng này đã hết.")
        try:
            gold_domains = ["sjc.com.vn", "doji.vn", "pnj.com.vn", "btmc.vn"] if "gia vang" in normalize_text(query) else []
            payload = {"query": query[:600], "search_depth": "basic", "auto_parameters": False,
                       "max_results": self.settings.information_max_results, "include_answer": False,
                       "include_raw_content": False, "include_published_date": True}
            if gold_domains:
                payload["include_domains"] = gold_domains
                payload["include_domains_mode"] = "restrict"
            data = await self.request("POST", "https://api.tavily.com/search", json=payload,
                                      headers={"Authorization": "Bearer " + self.settings.tavily_api_key})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403, 429, 432, 433}:
                self.search_unavailable_until = time.monotonic() + (300 if exc.response.status_code in {401, 403} else 60)
            raise
        results = []
        retrieved = datetime.now(UTC).isoformat()
        if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
            raise InformationError("search_response_invalid", "Dịch vụ tìm kiếm trả về dữ liệu chưa hợp lệ. Bạn thử lại nhé.")
        for item in data.get("results", [])[:self.settings.information_max_results]:
            if not isinstance(item, dict):
                continue
            url = public_url(item.get("url"))
            if url and gold_domains:
                host = urlsplit(url).hostname
                if not any(host == domain or host.endswith("." + domain) for domain in gold_domains):
                    continue
            content = plain(item.get("content"))[:4000]
            if url and content:
                results.append({"source_id": source_identifier("web", url, content), "kind": "web",
                                "title": plain(item.get("title"))[:200], "source_url": url,
                                "content": content, "published_at": item.get("published_date"), "retrieved_at": retrieved})
        return results

    async def wikipedia(self, query):
        for language in ("vi", "en"):
            data = await self.request("GET", f"https://{language}.wikipedia.org/w/api.php", params={
                "action": "query", "format": "json", "generator": "search", "gsrsearch": query[:300],
                "gsrlimit": 3, "prop": "extracts|info", "exintro": 1, "explaintext": 1,
                "exlimit": "max", "inprop": "url", "formatversion": 2,
            })
            results = []
            for item in sorted(data.get("query", {}).get("pages", []), key=lambda item: item.get("index", 0)):
                url = public_url(item.get("fullurl"))
                content = plain(item.get("extract"))[:4000]
                if url and content:
                    results.append({"source_id": source_identifier("wiki", url, content), "kind": "wikipedia",
                                    "title": item.get("title", "")[:200], "source_url": url,
                                    "content": content, "retrieved_at": datetime.now(UTC).isoformat()})
            if results:
                return results
        return []

    async def weather(self, arguments, event, session):
        location = arguments.get("location")
        query = resolve_query(arguments["query"], session)
        value = normalize_text(query)
        day = arguments.get("forecast_day", "today")
        target = None
        choices = session.conversation_context.get("location_candidates", [])
        target = next((p for p in choices if p.get("label") == location), None)
        previous = session.conversation_context
        if not target and location and location == previous.get("location") and previous.get("position"):
            target = {**previous["position"], "timezone": previous.get("timezone") or self.settings.information_timezone}
        selected_destination = destination(session)
        if not target and location and normalize_text(location) == normalize_text(selected_destination.get("name", "")):
            target = selected_destination
        if location and normalize_text(location) in {"o do", "tai do", "noi sap den", "cho sap toi"}:
            target = destination(session)
            location = target.get("name")
        if not location and re.search(r"o do|tai do|noi sap den|cho sap toi", normalize_text(arguments["query"])):
            target = destination(session)
            location = target.get("name")
        if not location and re.search(r"o day|hien tai|bay gio", value):
            target = event.position
        if not location and not target:
            # A short date follow-up inherits location, never arbitrary old GPS.
            context = session.conversation_context
            if context.get("capability") == "weather.get_forecast":
                location = context.get("location")
                target = context.get("position") if not location else None
        if not location and not target:
            target = event.position
        if not location and not target:
            return {"status": "waiting_slot", "message": "Bạn muốn xem thời tiết ở đâu?",
                    "information_context": {"capability": "weather.get_forecast", "forecast_day": day}, "error_code": "location_required"}
        if target and "lat" in target and "lng" in target:
            lat, lng = target["lat"], target["lng"]
            label = location or target.get("label") or target.get("name") or "vị trí hiện tại"
            timezone = target.get("timezone") or self.settings.information_timezone
        else:
            geocode_query = re.sub(r"^(?:tỉnh|thành phố|tp\.?|huyện)\s+", "", location, flags=re.I)
            geo = await self.request("GET", "https://geocoding-api.open-meteo.com/v1/search",
                                     params={"name": geocode_query, "count": 5, "language": "vi", "format": "json"})
            places = geo.get("results", [])
            local = [p for p in places if p.get("country_code") == "VN"]
            places = local or places
            if not places:
                return {"status": "waiting_slot", "message": "Mình chưa xác định được địa điểm đó. Bạn cho biết thành phố hoặc tỉnh nhé.",
                        "information_context": {"capability": "weather.get_forecast", "forecast_day": day}}
            if len(places) > 1:
                exact = [p for p in places if normalize_text(p.get("name", "")) == normalize_text(location)]
                # A capital/administrative city owns an unqualified city name;
                # homonymous villages should not force another voice turn.
                cities = [p for p in exact if p.get("feature_code") in {"PPLC", "PPLA"}]
                if len(exact) == 1 or len(cities) == 1:
                    places = cities or exact
                else:
                    labels = [", ".join(dict.fromkeys(str(p[k]) for k in ("name", "admin1", "country") if p.get(k))) for p in places[:3]]
                    candidates = [{"label": label, "name": p["name"], "lat": p["latitude"], "lng": p["longitude"],
                                   "timezone": p.get("timezone") or self.settings.information_timezone}
                                  for label, p in zip(labels, places[:3], strict=True)]
                    return {"status": "waiting_slot", "message": "Có nhiều địa điểm cùng tên: " + "; ".join(labels) + ". Bạn muốn nơi nào?",
                            "information_context": {"capability": "weather.get_forecast", "forecast_day": day,
                                                    "location_candidates": candidates}}
            place = places[0]
            lat, lng = place["latitude"], place["longitude"]
            label = place["name"]
            timezone = place.get("timezone") or self.settings.information_timezone
        params = {"latitude": lat, "longitude": lng, "timezone": timezone, "forecast_days": 3,
                  "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"}
        data = await self.request("GET", "https://api.open-meteo.com/v1/forecast", params=params)
        actual_timezone = data.get("timezone") or timezone
        requested_date = (datetime.now(ZoneInfo(actual_timezone)).date() + timedelta(days={"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}[day])).isoformat()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        if requested_date not in dates:
            raise InformationError("forecast_missing", "Nguồn thời tiết chưa có dự báo cho ngày bạn hỏi.")
        index = dates.index(requested_date)
        units = data.get("daily_units", {})
        if units.get("temperature_2m_min") != "°C" or units.get("temperature_2m_max") != "°C":
            raise InformationError("forecast_units_invalid", "Mình chưa xác minh được đơn vị của dự báo thời tiết.")
        low, high = daily["temperature_2m_min"][index], daily["temperature_2m_max"][index]
        rain = daily.get("precipitation_probability_max", [None] * len(dates))[index]
        if low is None or high is None:
            raise InformationError("forecast_missing", "Nguồn chưa có số liệu nhiệt độ cho ngày bạn hỏi.")
        label_day = {"today": "hôm nay", "tomorrow": "ngày mai", "day_after_tomorrow": "ngày kia"}[day]
        message = f"Dự báo tại {label} {label_day} ({requested_date}): nhiệt độ từ {low:g} đến {high:g} độ C."
        if rain is not None and units.get("precipitation_probability_max") == "%":
            message += f" Khả năng có mưa cao nhất trong ngày là {rain:g}%."
        retrieved = datetime.now(UTC).isoformat()
        citation = {"source_id": source_identifier("weather", lat, lng, requested_date), "kind": "weather", "title": "Open-Meteo · " + label,
                    "source_url": "https://open-meteo.com/en/docs", "retrieved_at": retrieved,
                    "valid_date": requested_date, "timezone": actual_timezone}
        return {"status": "completed", "message": message, "answer_kind": "weather", "answer_basis": "sources",
                "grounding_status": "supported", "citations": [citation], "information_provider": "open_meteo",
                "information_context": {"capability": "weather.get_forecast", "location": label if location else None,
                                        "position": {"lat": lat, "lng": lng}, "forecast_day": day, "timezone": actual_timezone},
                "data": {"location": label, "date": requested_date, "timezone": actual_timezone,
                         "temperature_min_c": low, "temperature_max_c": high, "rain_probability_max": rain}}

    async def execute(self, capability, arguments, event, session, *, provider, history=None, memory=None):
        if not self.online:
            return {"status": "blocked", "message": "Mình đang không truy cập internet nên chưa lấy được thông tin mới.", "error_code": "internet_disabled"}
        query = resolve_query(arguments["query"], session)
        # Avoid carrying memory or full conversation into provider requests.
        time_context = search_time_context(query, self.settings.information_timezone)
        information_context = {"capability": capability, "query": query, "topic_query": search_topic(query, session)}
        weather_scope = None
        if capability == "weather.get_forecast":
            weather_scope = {"position": {k: event.position[k] for k in ("lat", "lng") if k in event.position} if event.position else None,
                             "destination": destination(session), "previous_location": session.conversation_context.get("location"),
                             "previous_position": session.conversation_context.get("position")}
        key = hashlib.sha256(json.dumps([capability, query, arguments.get("location"), arguments.get("forecast_day"), weather_scope,
                                         time_context["current_date"]], sort_keys=True).encode()).hexdigest()
        cached = self.cache.get(key)
        fetched = None
        cache_hit = False
        if cached and capability == "weather.get_forecast":
            forecast = cached[1].get("data", {})
            local_day = datetime.now(ZoneInfo(forecast.get("timezone") or self.settings.information_timezone)).date()
            offset = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}[arguments.get("forecast_day", "today")]
            if forecast.get("date") != (local_day + timedelta(days=offset)).isoformat():
                cached = None
        try:
            async with asyncio.timeout(self.settings.information_timeout_seconds):
                if cached and cached[0] > time.monotonic():
                    fetched = copy.deepcopy(cached[1])
                    cache_hit = True
                else:
                    cache_hit = False
                    if capability == "weather.get_forecast":
                        fetched = await self.weather(arguments, event, session)
                    else:
                        fetched = await (self.search(time_context["search_query"]) if capability == "web.search" else self.wikipedia(query))
                    if not isinstance(fetched, dict) or fetched.get("status") == "completed":
                        if len(self.cache) >= 128:
                            self.cache.pop(next(iter(self.cache)))
                        ttl = 60 if "gia vang" in normalize_text(query) else 600 if capability == "weather.get_forecast" else 86400 if capability == "knowledge.lookup" else 300
                        self.cache[key] = (time.monotonic() + ttl, copy.deepcopy(fetched))
                if capability == "weather.get_forecast":
                    return {**fetched, "cache_hit": cache_hit}
                source_metadata = {"information_provider": "tavily" if capability == "web.search" else "wikipedia",
                                   "cache_hit": cache_hit, "source_count": len(fetched), "information_context": information_context}
                if not fetched:
                    return {**source_metadata, "status": "blocked", "message": "Mình chưa tìm được nguồn phù hợp để trả lời câu này.", "error_code": "no_evidence", "grounding_status": "insufficient_evidence"}
                if "gia vang" in normalize_text(query):
                    today = date.fromisoformat(time_context["current_date"])
                    dates = {today.isoformat(), today.strftime("%d/%m/%Y"), f"{today.day}/{today.month}/{today.year}"}
                    date_pattern = r"(?<!\d)(?:" + "|".join(re.escape(value) for value in dates) + r")(?!\d)"
                    current = [item for item in fetched if re.search(date_pattern, item["content"])]
                    if not current:
                        return {**source_metadata, "status": "blocked", "message": "Mình tìm được nguồn giá vàng nhưng chưa xác minh được báo giá hôm nay. Bạn có thể xem nguồn trên màn hình.",
                                "error_code": "current_quote_not_verified", "answer_kind": "web", "grounding_status": "insufficient_evidence",
                                "answer_basis": "sources",
                                "citations": [{k: v for k, v in item.items() if k != "content"} for item in fetched]}
                    fetched = current
                result = await self.conversation.respond(query, provider=provider, evidence=fetched, history=history, memory=memory,
                                                         context=time_context)
                return {**result, "answer_kind": "web" if capability == "web.search" else "knowledge",
                        "information_provider": "tavily" if capability == "web.search" else "wikipedia",
                        "cache_hit": cache_hit, "source_count": len(fetched),
                        "information_context": information_context}
        except TimeoutError:
            if isinstance(fetched, list) and fetched:
                return {"status": "blocked", "message": "Mình đã tìm được nguồn nhưng chưa tổng hợp kịp. Bạn có thể xem nguồn trên màn hình hoặc hỏi lại nhé.",
                        "error_code": "information_timeout", "answer_kind": "web" if capability == "web.search" else "knowledge",
                        "answer_basis": "sources", "grounding_status": "insufficient_evidence",
                        "citations": [{k: v for k, v in item.items() if k != "content"} for item in fetched],
                        "information_provider": "tavily" if capability == "web.search" else "wikipedia",
                        "source_count": len(fetched), "cache_hit": cache_hit, "information_context": information_context}
            return {"status": "blocked", "message": "Nguồn internet chưa phản hồi kịp. Bạn thử lại nhé.",
                    "error_code": "information_timeout", "grounding_status": "insufficient_evidence"}
        except InformationError as exc:
            return {"status": "blocked", "message": exc.message, "error_code": exc.code}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            return {"status": "blocked", "message": "Mình chưa lấy được dữ liệu đáng tin cậy từ nguồn internet. Bạn thử lại sau nhé.",
                    "error_code": "information_unavailable", "grounding_status": "insufficient_evidence"}
