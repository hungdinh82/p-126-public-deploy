from __future__ import annotations

from pathlib import Path

from src.vivi.ingestion.crawler import CrawlTarget
from src.vivi.rag.scope import scope_rejection_reason
from tests.support.rag import FakeGenerator as FakeGenerator
from tests.support.rag import FakeRetriever as FakeRetriever
from tests.support.rag import _chunk as _chunk


def _input(query: str) -> dict:
    return {"capability": "manual.search", "arguments": {"query": query}}


def test_scope_guard_rejects_sales_and_unsafe_repairs():
    assert scope_rejection_reason("VF8 đang khuyến mãi bao nhiêu?")
    assert scope_rejection_reason("Hướng dẫn tôi bypass hệ thống pin cao áp")
    assert scope_rejection_reason("Cách bật điều hòa là gì?") is None
    assert scope_rejection_reason("Thời tiết hôm nay thế nào?")


def test_crawl_target_maps_locale_and_path():
    target = CrawlTarget()
    assert target.language_country == ("vi", "vn")
    assert target.slug == Path("vf8/2026/vi_vn")
