"""Local query normalization: keep topic words and expand handbook terminology."""

import re

from src.vivi.text import normalize_text

_FUNCTION_WORDS = set("ban minh toi xe vf8 cua co la gi nao the nhu sao bao nhieu hay va voi ve cho mot nhung cac nay do khi o duoc de can thong tin tinh nang huong dan su dung cach".split())


def expand_query(query: str) -> str:
    normalized = normalize_text(query)
    if "cruise control" in normalized:
        query += " kiểm soát hành trình thích ứng ACC"
    if re.search(r"\badas\b", normalized):
        query += " hệ thống hỗ trợ người lái"
    return query


def lexical_query(query: str) -> str:
    words = re.findall(r"[^\W_]+", query, re.UNICODE)
    selected = [word for word in words if normalize_text(word) not in _FUNCTION_WORDS]
    return " ".join(selected) or query
