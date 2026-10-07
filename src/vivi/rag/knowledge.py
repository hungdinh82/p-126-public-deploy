"""Shared handbook vocabulary and question contract for routing and retrieval.

Aliases identify topics, not answers. All facts still come from scoped handbook
chunks. Specific topics precede their parent so 'cửa sổ' isn't a door command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from src.vivi.text import normalize_text

QuestionKind = Literal[
    "definition",
    "availability",
    "overview",
    "procedure",
    "specification",
    "duration",
    "troubleshooting",
    "limitation",
    "artifact",
    "categorical",
    "general",
]


@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    aliases: tuple[str, ...]
    evidence: str
    group: str = "cabin"
    umbrella: bool = False

    def matches(self, text: str) -> bool:
        return any(re.search(r"\b" + re.escape(normalize_text(alias)) + r"\b", text) for alias in self.aliases)


TOPICS = (
    Topic(
        "phone_charging",
        "sạc điện thoại",
        ("sạc không dây", "sạc điện thoại", "sạc thiết bị", "sạc usb"),
        r"sac.*(?:khong day|dien thoai)|usb",
    ),
    Topic("lane_keep", "hỗ trợ giữ làn đường (LKA)", ("LKA", "giữ làn", "trợ làn"), r"\blka\b|giu lan|tro lan", "adas"),
    Topic(
        "lane_warning",
        "cảnh báo chệch làn (LDW)",
        ("LDW", "chệch làn", "lệch làn"),
        r"\bldw\b|chech lan|lech lan",
        "adas",
    ),
    Topic(
        "acc",
        "kiểm soát hành trình thích ứng (ACC)",
        ("ACC", "cruise control", "ga tự động", "kiểm soát hành trình", "điều khiển hành trình"),
        r"\bacc\b|hanh trinh|cruise",
        "adas",
    ),
    Topic(
        "isa",
        "điều chỉnh tốc độ thông minh",
        ("ISA", "điều chỉnh tốc độ thông minh"),
        r"\bisa\b|dieu chinh toc do thong minh",
        "adas",
    ),
    Topic("aeb", "phanh khẩn cấp tự động (AEB)", ("AEB", "phanh khẩn cấp"), r"\baeb\b|phanh khan cap", "adas"),
    Topic("abs", "chống bó cứng phanh (ABS)", ("ABS", "chống bó cứng"), r"\babs\b|chong bo cung", "safety"),
    Topic(
        "adas",
        "hỗ trợ lái nâng cao (ADAS)",
        ("ADAS", "hỗ trợ người lái", "tự lái", "tính năng thông minh"),
        r"\badas\b|ho tro (?:nguoi )?lai|tro giup lai",
        "adas",
        True,
    ),
    Topic("hud", "hiển thị trên kính lái (HUD)", ("HUD", "hiển thị trên kính"), r"\bhud\b|hien thi tren kinh|head.?up"),
    Topic(
        "vehicle_start",
        "khởi động xe",
        ("khởi động xe", "khởi động VF8", "đề máy"),
        r"khoi dong|bat.*nguon|nut.*nguon",
        "operation",
    ),
    Topic(
        "vehicle_dimensions",
        "kích thước xe",
        ("chiều dài xe", "chiều cao xe", "chiều rộng xe", "khoảng cách trục", "trục bánh xe", "khoảng sáng gầm"),
        r"kich thuoc|chieu dai|chieu cao|chieu rong|khoang cach truc|khoang sang",
    ),
    Topic(
        "child_safety",
        "an toàn trẻ em",
        ("trẻ em", "trẻ sơ sinh", "trẻ nhỏ", "ghế trẻ em", "CRS"),
        r"tre em|tre nho|\bcrs\b",
        "safety",
    ),
    Topic("tire_pressure", "áp suất lốp", ("áp suất lốp",), r"ap suat|\btpms\b"),
    Topic("tire", "lốp xe", ("lốp", "bánh xe"), r"lop|banh xe"),
    Topic("ev_charging", "sạc pin xe", ("sạc", "cổng sạc"), r"sac|pin", "energy"),
    Topic("battery", "pin xe", ("pin", "SDI", "CATL", "ắc quy", "bình điện"), r"pin|sdi|catl|ac quy", "energy"),
    Topic("range", "quãng đường còn lại", ("quãng đường", "còn đi được", "đi thêm"), r"quang duong|pin", "energy"),
    Topic(
        "climate",
        "điều hoà",
        ("điều hoà", "nhiệt độ", "ấm hơn", "mát hơn"),
        r"dieu hoa|nhiet do|lam mat|suoi|chat lam lanh",
    ),
    Topic("window", "cửa sổ", ("cửa sổ", "cửa kính", "kính"), r"cua so|cua kinh|kinh"),
    Topic(
        "door", "cửa xe", ("cửa xe", "mở cửa", "đóng cửa", "cửa bên", "khóa cửa", "khoá xe", "chìa khoá"), r"cua|khoa"
    ),
    Topic("seat", "ghế", ("ghế",), r"ghe"),
    Topic("mirror", "gương", ("gương",), r"guong"),
    Topic("lights", "đèn", ("đèn",), r"den"),
    Topic("brakes", "phanh", ("phanh",), r"phanh", "safety"),
    Topic("airbag", "túi khí", ("túi khí", "SRS"), r"tui khi|srs", "safety"),
    Topic("wifi", "Wi-Fi", ("wifi", "wi-fi"), r"wi.?fi|mang"),
    Topic("bluetooth", "Bluetooth", ("bluetooth", "carplay", "android auto"), r"bluetooth|carplay|android auto"),
    Topic("display", "màn hình", ("màn hình",), r"man hinh"),
    Topic("media", "âm nhạc", ("nhạc", "âm thanh"), r"am thanh|nhac"),
    Topic("camera", "camera", ("camera",), r"camera"),
    Topic("camping", "chế độ cắm trại", ("cắm trại", "camping"), r"cam trai|camping"),
    Topic("maintenance", "bảo dưỡng", ("bảo dưỡng", "dầu phanh", "dầu động cơ", "gạt nước"), r"bao duong|dau|gat nuoc"),
    Topic("steering", "vô lăng", ("vô lăng",), r"vo lang", "safety"),
    Topic("trunk", "cốp", ("cốp",), r"cop"),
)
BY_KEY = {topic.key: topic for topic in TOPICS}


def find_topic(text: str) -> Topic | None:
    normalized = normalize_text(text)
    return next((topic for topic in TOPICS if topic.matches(normalized)), None)


@dataclass(frozen=True)
class KnowledgeQuestion:
    query: str
    topic: Topic | None
    kind: QuestionKind

    @property
    def retrieval_query(self) -> str:
        if self.topic is None:
            return self.query
        # Add precise handbook terminology once, without an entire previous turn.
        if normalize_text(self.topic.label) in normalize_text(self.query):
            return self.query
        return f"{self.query} {self.topic.label}"

    def as_dict(self) -> dict:
        return {"topic": self.topic.key if self.topic else None, "question_kind": self.kind}


@lru_cache(maxsize=4096)
def analyze_question(query: str) -> KnowledgeQuestion:
    text = normalize_text(query)
    topic = find_topic(query)
    kind: QuestionKind = "general"
    if re.search(r"ma nguon|source code", text):
        kind = "artifact"
    elif re.search(r"khong (?:mo|dong|hoat dong).*duoc|bi ket|khong hoat dong|truc trac", text):
        kind = "troubleshooting"
    elif re.search(r"thay the.*nguoi lai|tu lai|phu thuoc|khong can.*nguoi lai", text):
        kind = "limitation"
    elif "sac" in text and re.search(r"bao lau|thoi gian|bao nhieu (?:gio|phut)", text):
        kind = "duration"
    elif re.search(r"\b(cach|lam sao|huong dan|xu ly|su dung)\b|dung.*(?:nut|man hinh)|nut nao|buoc nao", text):
        kind = "procedure"
    elif re.search(r"loai.*(?:nao|la gi)|loai.*(?:moi chat|chat lam lanh)", text):
        kind = "categorical"
    elif re.search(
        r"kich thuoc|dung luong|dien ap|ap suat|cong suat|thong so|loai.*(?:moi chat|chat lam lanh)|(?:bao nhieu|may) (?:tui khi|banh|ghe)",
        text,
    ):
        kind = "specification"
    elif re.search(r"\bco\b.*\bkhong\b|duoc trang bi", text) and not re.search(
        r"\b(khi|luc|neu|can|hoat dong|tac dung)\b", text
    ):
        kind = "availability"
    elif re.search(r"\bla gi\b|tac dung|de lam gi|hoat dong the nao|gioi thieu|giai thich", text):
        kind = "definition"
    elif re.search(r"gom|co nhung|nhung.*tinh nang|tinh nang.*nao", text):
        kind = "overview"
    return KnowledgeQuestion(query, topic, kind)
