"""Resolve relative search dates using the configured local timezone."""

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.vivi.text import normalize_text


def search_time_context(query, timezone, *, now=None):
    local_day = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone)).date()
    next_month = (local_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    monday = local_day - timedelta(days=local_day.weekday())
    replacements = {
        "hom nay": f"ngày {local_day:%d/%m/%Y}",
        "ngay mai": f"ngày {local_day + timedelta(days=1):%d/%m/%Y}",
        "ngay kia": f"ngày {local_day + timedelta(days=2):%d/%m/%Y}",
        "thang nay": f"tháng {local_day.month}/{local_day.year}",
        "cuoi thang nay": f"cuối tháng {local_day.month}/{local_day.year}",
        "thang toi": f"tháng {next_month.month}/{next_month.year}",
        "cuoi thang toi": f"cuối tháng {next_month.month}/{next_month.year}",
        "nam nay": f"năm {local_day.year}",
        "tuan nay": f"tuần {monday:%d/%m/%Y} đến {monday + timedelta(days=6):%d/%m/%Y}",
        "cuoi tuan nay": f"cuối tuần {monday + timedelta(days=5):%d/%m/%Y} đến {monday + timedelta(days=6):%d/%m/%Y}",
    }
    resolved = unicodedata.normalize("NFC", query)
    matches = list(
        re.finditer(r"\b(?:" + "|".join(sorted(replacements, key=len, reverse=True)) + r")\b", normalize_text(resolved))
    )
    for match in reversed(matches):
        resolved = resolved[: match.start()] + replacements[match[0]] + resolved[match.end() :]
    return {"current_date": local_day.isoformat(), "timezone": timezone, "search_query": resolved}
