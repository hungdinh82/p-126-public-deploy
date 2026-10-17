"""Host vetoes for clauses the small proposal model cannot represent.

A guard may only withhold execution: a match never proposes an action, fills a
slot or picks a tool. Matching keeps Vietnamese diacritics because folded text
conflates meanings (đừng/dùng/đúng); unaccented input still reaches the model.
"""

import re
import unicodedata

NUMBER = r"(?:\d+|một|hai|ba|bốn|tư|năm|sáu|bảy|tám|chín|mười|mươi|lăm|rưỡi)"
CONTROL_VERB = (
    r"(?:bật|tắt|mở|đóng|khóa|khoá|hạ|nâng|kéo|tăng|giảm|đặt|chỉnh|phát|dừng|ngừng|"
    r"tạm dừng|chỉ đường|dẫn đường|sưởi|chuyển|cài)"
)
VETOES = (
    (
        "negated",
        re.compile(rf"(?<!\w)(?:đừng|chớ|không được|không cần|không {CONTROL_VERB})(?!\w)"),
        "Mình chưa xử lý lệnh có phần phủ định. Bạn nói riêng thao tác muốn làm nhé.",
    ),
    (
        "conditional",
        re.compile(r"(?<!\w)(?:nếu|hễ|trường hợp|khi nào|lúc nào)(?!\w)"),
        "Mình chưa hỗ trợ lệnh có điều kiện. Bạn nói thao tác muốn làm ngay nhé.",
    ),
    (
        "scheduled",
        re.compile(
            rf"(?<!\w)(?:hẹn giờ|lát nữa|(?:{NUMBER}\s+)+(?:phút|giờ|tiếng)\s+nữa|"
            rf"sau\s+(?:{NUMBER}\s+)+(?:phút|giờ|tiếng)|lúc\s+(?:{NUMBER}\s*)+(?:giờ|h))(?!\w)"
        ),
        "Mình chưa hỗ trợ hẹn giờ thao tác. Bạn nói lệnh muốn làm ngay nhé.",
    ),
)
SEPARATOR = re.compile(r"\s*(?:[,;]|(?<!\w)(?:và|rồi|sau đó|xong|với cả|với lại|đồng thời|nhưng|thêm nữa)(?!\w))\s*")
FILLER = re.compile(r"^(?:(?:hãy|làm ơn|vui lòng|giúp tôi|giúp mình|cho tôi|cho mình|vivi ơi|ok|thì|bạn)\s+)*")
VERB_LED = re.compile(rf"^{CONTROL_VERB}(?!\w)")
GREETING = re.compile(
    r"(?<!\w)(?:chào|xin chào|cảm ơn|cám ơn|tạm biệt|hello|hi|thanks|thank you|ok|oke|okay|tuyệt|giỏi)(?!\w)"
)


def spelling(text):
    return unicodedata.normalize("NFC", text).casefold()


def veto(text):
    """Return (flag, message) for a negated, conditional or scheduled command."""
    value = spelling(text)
    return next(((flag, message) for flag, pattern, message in VETOES if pattern.search(value)), None)


def action_clauses(text):
    """Clauses that start with a control verb; plain target lists are not clauses."""
    parts = [FILLER.sub("", part.strip(" .!?")) for part in SEPARATOR.split(spelling(text))]
    return [part for part in parts if VERB_LED.match(part)]


def greeting(text):
    return bool(GREETING.search(spelling(text)))


def strip_leading_greeting(text):
    """Drop a leading greeting clause when a real request follows it.

    "chào bạn, mở cốp" -> "mở cốp": the pleasantry is noise that pushes a small
    model to reply instead of acting. A bare greeting ("chào Vivi buổi sáng")
    has no following clause and is returned unchanged.
    """
    head, _, rest = text.partition(",")
    rest = rest.strip()
    if rest and greeting(head) and not action_clauses(head):
        return rest
    return text


class HostTurn(Exception):  # noqa: N818 - a control-flow signal, not a failure
    """A host-decided non-executing turn (clarify/unsupported/reply)."""

    def __init__(self, act, text, reason):
        super().__init__(reason)
        self.act, self.text, self.reason = act, text, reason
