from __future__ import annotations

import re
import unicodedata

_NON_TECHNICAL = re.compile(
    r"\b(gia ban|khuyen mai|dat coc|tra gop|co phieu|doanh thu|tin tuc|"
    r"so sanh.*(?:tesla|toyota|hyundai|kia)|mua xe o dau)\b"
)
_UNSAFE_REPAIR = re.compile(
    r"\b(vo hieu hoa|bypass|hack|do pin|mo pin|thao pin|sua pin cao ap|"
    r"can thiep dien cao ap|tat tui khi|tat he thong an toan)\b"
)
_TECHNICAL = re.compile(
    r"\b(vf\s*8|xe|pin|sac|cong sac|cua|khoa|ghe|den|lai|phanh|lop|banh|"
    r"guong|dieu hoa|man hinh|che do|canh bao|bao duong|dau|gat nuoc|vo lang|"
    r"tui khi|day dai|adas|camera|cop|dong co|cong suat|ap suat|am thanh|"
    r"bluetooth|wifi|ung dung|carplay|android auto|hud|hanh trinh|do xe|"
    r"kinh|tcs|abs|srs|luc keo|moi chat|cruise|cabin|cam nang|tu lai|quang duong|con di duoc|di them)\b"
)
_OTHER_VEHICLE = re.compile(r"\b(vf\s*(?:3|5|6|7|9)|tesla|toyota|hyundai|kia)\b")


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in value if unicodedata.category(char) != "Mn").replace("đ", "d")


def scope_rejection_reason(query: str) -> str | None:
    normalized = _normalize(query)
    if _OTHER_VEHICLE.search(normalized):
        return "Mình có thông tin về VF8 2026, chưa có thông tin chắc chắn về mẫu xe bạn hỏi."
    if _UNSAFE_REPAIR.search(normalized):
        return "Mình không thể hướng dẫn can thiệp hệ thống an toàn hoặc điện cao áp."
    if _NON_TECHNICAL.search(normalized):
        return "Mình có thể giúp bạn dùng các tính năng trên xe; phần này mình chưa có thông tin."
    if not _TECHNICAL.search(normalized):
        return "Bạn muốn tìm hiểu tính năng nào trên chiếc VF8 của mình?"
    return None
