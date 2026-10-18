"""Find tool documentation, never interpret arguments or authorize an action."""

import re
import unicodedata

from src.vivi.text import normalize_text

MAX_TOOL_DOCUMENTS = 8

# Positive discovery vocabulary. Unlike an intent parser these tags carry no
# arguments, defaults, task changes or execution decisions. A model may request
# more documentation if this initial shortlist is insufficient.
DISCOVERY_TERMS = {
    "conversation.respond": (
        "nói chuyện",
        "tâm sự",
        "kể chuyện",
        "kể một",
        "viết bài",
        "làm thơ",
        "bài thơ",
        "sốt",
        "bệnh",
        "đau",
        "bạn là ai",
        "thứ mấy",
        "mấy giờ",
    ),
    "web.search": (
        "trên mạng",
        "internet",
        "hiện nay",
        "hiện tại",
        "giá vàng",
        "tin tức",
        "âm lịch",
        "tỷ giá",
        "giá xăng",
    ),
    "weather.get_forecast": ("thời tiết", "trời mưa", "dự báo", "nắng không", "có mưa", "mưa không"),
    "knowledge.lookup": ("là gì", "giải thích", "tại sao", "lịch sử"),
    "memory.recall": ("nhớ gì", "đã nhớ", "tên tôi"),
    "memory.remember": ("ghi nhớ", "hãy nhớ", "nhớ rằng", "tôi tên", "gọi tôi"),
    "memory.forget": ("quên", "xóa ghi nhớ"),
    "memory.reset": ("quên hết", "xóa hết"),
    "manual.search": ("cẩm nang", "hướng dẫn", "thông số", "sạc bao lâu", "cách dùng"),
    # "cửa đang mở" in a command is a door filter, not a status question.
    "vehicle.get_status": ("trạng thái", "kiểm tra", "pin còn", "bao nhiêu độ", "đang mở không", "đã đóng chưa"),
    "climate.set_temperature": (
        "nhiệt độ",
        "điều hòa",
        "điều hoà",
        "lạnh",
        "nóng",
        "nực",
        "làm mát",
        "mát hơn",
        "ấm hơn",
        "tăng thêm",
        "giảm bớt",
        "tăng tiếp",
        "giảm tiếp",
        # "X độ" is a temperature in car control; the accent guard keeps "đó" out.
        "độ",
    ),
    "window.set_position": ("cửa sổ", "cửa kính", "kính"),
    "door.set_open": ("cửa", "cánh cửa"),
    "door.set_lock": ("khóa", "khoá"),
    "hood.set_open": ("capo", "ca pô", "ca-pô"),
    "trunk.set_open": ("cốp", "cửa hậu"),
    "body.set_open": ("tất cả cửa nắp capo và cốp", "toàn bộ cửa nắp capo và cốp"),
    "seat.set_heat_level": ("sưởi ghế",),
    "media.play": (
        "nhạc",
        "bài hát",
        "nghe bài",
        "phát bài",
        "mở bài",
        "bật bài",
        "ca sĩ",
        "radio",
        "âm thanh",
        "play music",
        "phát lại nhạc",
        "tiếp tục nhạc",
    ),
    "media.pause": ("dừng nhạc", "tắt nhạc", "tạm dừng nhạc", "ngừng nhạc", "pause music"),
    "navigation.set_gear": ("số p", "số d", "chuyển số", "vào số"),
    "navigation.search": (
        "tìm",
        "gần đây",
        "ở đâu",
        "gần nhất",
        "gần tôi",
        "gần mình",
        "quanh đây",
        "gần hơn",
        # "vị trí hiện tại" is a place, not a request for current information.
        "vị trí hiện tại",
        "bệnh viện",
        "phòng khám",
        "cấp cứu",
        "nhà thuốc",
        "hiệu thuốc",
        "quán",
        "cà phê",
        "cafe",
        "nhà hàng",
        "cây xăng",
        "trạm xăng",
        "trạm sạc",
        "bãi đỗ",
        "chỗ đỗ xe",
        "siêu thị",
        "khách sạn",
        "nhà vệ sinh",
    ),
    "navigation.refine": (
        "đổi về",
        "đổi sang",
        "đổi thành",
        "đổi địa điểm",
        "đổi điểm đến",
        "chỗ khác",
        "nơi khác",
        "gần hơn",
        "xa quá",
    ),
    "navigation.cancel": ("hủy", "huỷ", "hủy tuyến", "huỷ tuyến", "hủy kế hoạch", "huỷ kế hoạch", "không đi nữa"),
    "navigation.plan": ("chỉ đường", "dẫn đường", "đường đến", "đi tới", "đến"),
    "trip.plan": ("hành trình", "chuyến đi", "khứ hồi", "lập kế hoạch"),
    "navigation.start": ("bắt đầu dẫn đường", "bắt đầu đi"),
    "navigation.stop": ("dừng dẫn đường",),
    "navigation.end": (
        "kết thúc dẫn đường",
        "kết thúc hành trình",
        "kết thúc lộ trình",
        "hủy lộ trình",
        "huỷ lộ trình",
        "hủy hành trình",
        "huỷ hành trình",
    ),
    "trip.start": ("bắt đầu hành trình",),
    "trip.cancel": ("hủy hành trình",),
}


def discover_capabilities(text, capabilities):
    # No match is not evidence of unsupported intent: retain the complete catalog.
    return matched_capabilities(text, capabilities) or capabilities


def matched_capabilities(text, capabilities):
    spelling = unicodedata.normalize("NFC", text.lower())
    text = normalize_text(spelling)
    matches = []
    for item in capabilities:
        terms = (*DISCOVERY_TERMS.get(item["name"], ()), item["name"], item["name"].replace(".", "_"))
        for term in terms:
            term = unicodedata.normalize("NFC", term.lower())
            folded = normalize_text(term)
            for match in re.finditer(r"(?<!\w)" + re.escape(folded) + r"(?!\w)", text):
                # Missing accents remain discoverable, but explicit different
                # accents carry meaning: đèn (lamp) is not đến (destination).
                # Only use aligned characters when normalization kept length.
                if (
                    len(spelling) == len(text)
                    and len(term) == len(folded)
                    and any(
                        actual != expected and actual != normalize_text(actual) and expected != normalize_text(expected)
                        for actual, expected in zip(spelling[match.start() : match.end()], term, strict=True)
                    )
                ):
                    continue
                matches.append((match.start(), match.end(), item["name"]))
    names = {
        name
        for start, end, name in matches
        if not any(
            other_start <= start and end <= other_end and (other_start < start or end < other_end)
            for other_start, other_end, _ in matches
        )
    }
    return [item for item in capabilities if item["name"] in names]


def initial_tool_documents(text, capabilities):
    selected = discover_capabilities(text, capabilities)
    if len(selected) <= MAX_TOOL_DOCUMENTS:
        return selected, None
    # Progressive disclosure: expose a compact index rather than all navigation
    # schemas when lexical discovery is inconclusive. No capability disappears;
    # the model can explicitly fetch its documentation by name.
    core = {"conversation.respond", "knowledge.lookup", "web.search"}
    index = [{"name": item["name"], "description": item.get("description", "")[:140]} for item in capabilities]
    return [item for item in capabilities if item["name"] in core], index
