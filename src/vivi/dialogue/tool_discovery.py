"""Find tool documentation, never interpret arguments or authorize an action."""

import re
import unicodedata

from src.vivi.dialogue.proposal_contracts import is_status_question
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
        "tin",
        "mới nhất",
        "cập nhật",
        "tra cứu",
    ),
    "weather.get_forecast": ("thời tiết", "trời mưa", "dự báo", "nắng không", "có mưa", "mưa không"),
    "knowledge.lookup": ("là gì", "giải thích", "tại sao", "lịch sử"),
    "memory.recall": ("nhớ gì", "đã nhớ", "tên", "ghi nhớ", "sở thích"),
    "memory.remember": ("ghi nhớ", "hãy nhớ", "nhớ rằng", "tôi tên", "gọi tôi"),
    "memory.forget": ("quên", "xóa ghi nhớ"),
    "memory.reset": ("quên hết", "xóa hết", "reset", "bộ nhớ"),
    "manual.search": ("cẩm nang", "hướng dẫn", "thông số", "sạc bao lâu", "cách dùng"),
    # "cửa đang mở" in a command is a door filter, not a status question.
    "vehicle.get_status": (
        "trạng thái",
        "tình trạng",
        "kiểm tra",
        "pin còn",
        "pin còn lại",
        "phần trăm pin",
        "áp suất",
        "lốp",
        "nhiệt độ cabin",
        "bao nhiêu độ",
        "đang mở không",
        "đã đóng chưa",
        "đi được bao xa",
        "đi được bao lâu",
        "quãng đường còn lại",
        "mã lỗi",
        "thông tin xe",
        "thông tin của xe",
        "tổng quan xe",
        "tất cả thông tin",
        "xe",
        "cabin",
        "pin",
        "quãng đường",
        "di chuyển",
        "mã lỗi",
    ),
    "climate.set_temperature": (
        "nhiệt độ",
        "điều hòa",
        "điều hoà",
        "lạnh",
        "nóng",
        "nực",
        "oi",
        "oi bức",
        "lành lạnh",
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
    "door.set_lock": ("khóa", "khoá", "khóa cửa", "khoá cửa"),
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
    "media.pause": ("dừng nhạc", "tắt nhạc", "tạm dừng nhạc", "ngừng nhạc", "ngưng nhạc", "pause music", "stop music"),
    "navigation.set_gear": ("số p", "số d", "chuyển số", "vào số", "cần số"),
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
    "navigation.cancel": (
        "hủy",
        "huỷ",
        "hủy tuyến",
        "huỷ tuyến",
        "hủy kế hoạch",
        "huỷ kế hoạch",
        "hủy bản nháp",
        "hủy hành trình",
        "không đi nữa",
    ),
    "navigation.plan": (
        "chỉ đường",
        "dẫn đường",
        "đường đến",
        "đi tới",
        "đến",
        "hành trình",
        "chuyến đi",
        "khứ hồi",
        "lập kế hoạch",
    ),
    "navigation.start": ("bắt đầu dẫn đường", "bắt đầu đi", "bắt đầu hành trình"),
    "navigation.stop": ("dừng dẫn đường", "dừng hành trình", "dừng chuyến đi"),
    "navigation.end": (
        "kết thúc dẫn đường",
        "kết thúc hành trình",
        "kết thúc chuyến đi",
        "kết thúc lộ trình",
        "hủy lộ trình",
        "huỷ lộ trình",
        "hủy hành trình",
        "huỷ hành trình",
    ),
}

# Discovery offers alternatives within a domain; it never chooses a polarity,
# fills arguments or executes a command. Exact keyword matches must not make
# the opposite operation or a read tool inaccessible to the interpreter.
DOCUMENT_FAMILIES = (
    frozenset({"media.play", "media.pause"}),
    frozenset({"memory.remember", "memory.recall", "memory.forget", "memory.reset"}),
)

READ_CUES = re.compile(
    r"\b(?:doc|hoi|xem|biet|kiem tra|trang thai|tinh trang|bao nhieu|the nao|hay dong|hay mo|chua|khong)\b"
)
VEHICLE_CONTROLS = {
    "door.set_open",
    "door.set_lock",
    "window.set_position",
    "hood.set_open",
    "trunk.set_open",
    "climate.set_temperature",
}


def discover_capabilities(text, capabilities):
    # No match is not evidence of unsupported intent: retain the complete catalog.
    matched = matched_capabilities(text, capabilities)
    if not matched:
        return capabilities
    names = {item["name"] for item in matched}
    if names & VEHICLE_CONTROLS and not is_status_question(text):
        names.discard("vehicle.get_status")
    # A status match alone does not expose every actuator. Expansion is based
    # on the original matches, not transitively through the shared read tool.
    for family in DOCUMENT_FAMILIES:
        if (family - {"vehicle.get_status"}) & names:
            names |= family
    # Read documentation accompanies a vehicle topic when the utterance has
    # an inquiry cue. Do not flood a bare actuator command with twelve reads.
    if names & VEHICLE_CONTROLS and is_status_question(text):
        names.add("vehicle.get_status")
    return [item for item in capabilities if item["name"] in names]


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
