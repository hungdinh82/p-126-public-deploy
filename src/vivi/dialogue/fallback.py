"""Whole-utterance recovery grammar; never interprets a new compound request."""

import re
import unicodedata
from datetime import UTC, datetime

from src.vivi.dialogue.contracts import QuestionReply, SemanticTurn
from src.vivi.text import normalize_text

# Whole-utterance cancel phrases. Accent folding is safe here because the
# match must cover the complete reply ("huỷ", "huỷ kế hoạch", "thôi không đi nữa").
CANCEL_PATTERN = re.compile(
    r"(?:thoi |vay |thoi thi )?"
    r"(?:huy|huy bo|huy het|huy di|huy tuyen|huy tuyen duong|huy ke hoach|huy lo trinh|huy yeu cau|huy thao tac"
    r"|huy chi duong|huy dan duong|huy tim kiem|bo di|bo qua|khong di nua|khong can nua|thoi khong can"
    r"|thoi khong di|thoi huy|thoi bo di)"
    r"(?: di| nhe| nha| luon| a)?"
)
DENY_VALUES = {
    "khong",
    "chua",
    "khong dung",
    "de sau",
    "khong xac nhan",
    "khong phai",
    "khong phai cho nay",
    "sai roi",
    "thoi",
    "thoi khong",
    "khong can",
    "khong muon",
}


def folded_reply(text):
    raw = " ".join(unicodedata.normalize("NFC", text.casefold()).strip(" .,!?:;…").split())
    return raw, normalize_text(raw)


END_PATTERN = re.compile(
    r"(?:thoi |vay )?(?:huy|huy bo|ket thuc|dung han)"
    r" (?:lo trinh|hanh trinh|chuyen di|dan duong|chi duong|tuyen duong)(?: hien tai| nay| dang di)?(?: di| nhe| nha| luon)?"
)


def literal_cancel(text, session):
    """A bare cancel ends the pending task or draft without asking a model."""
    _, value = folded_reply(text)
    workspace = session.navigation_workspace
    idle_task = session.active is None or session.active.current_index() is None
    if idle_task and workspace.open_draft is None and workspace.live_journey and END_PATTERN.fullmatch(value):
        # "Huỷ lộ trình hiện tại" with only a trip running ends that trip;
        # navigation.end keeps its own confirmation policy.
        return SemanticTurn(act="request", actions=[{"capability": "navigation.end"}])
    if not (CANCEL_PATTERN.fullmatch(value) or END_PATTERN.fullmatch(value)):
        return None
    if idle_task and workspace.open_draft is None:
        return None
    return SemanticTurn(act="cancel", task_ref="active")


NEARER_PATTERN = re.compile(r"\b(?:gan hon|nao gan nhat|cho gan nhat|gan xe hon)\b")


def literal_nearer(text, session):
    """ "Chỗ nào gần hơn?" over a nearest-first list: say they already are."""
    from src.vivi.navigation.categories import CATEGORIES
    from src.vivi.navigation.presentation import choice_message

    draft = session.navigation_workspace.open_draft
    destination = draft.destination if draft else None
    _, value = folded_reply(text)
    if not (
        NEARER_PATTERN.search(value)
        and len(value.split()) <= 10
        and draft
        and draft.candidates
        and destination
        and destination.nearest
        and destination.entity_type in CATEGORIES
    ):
        return None
    label = CATEGORIES[destination.entity_type].label
    return SemanticTurn(act="reply", text=f"Đây đã là các {label} gần xe nhất. " + choice_message(draft.candidates))


ORDINALS = {"nhat": 1, "mot": 1, "dau tien": 1, "dau": 1, "hai": 2, "nhi": 2, "ba": 3, "bon": 4, "tu": 4, "nam": 5}
CHOICE_PATTERN = re.compile(
    r"(?:(?:toi |minh )?(?:chon|lay|di toi|di den|di|den|cho) )?"
    r"(?P<noun>(?:cai|cho|dia diem|ket qua|lua chon|phuong an|so|tram|quan) )?"
    r"(?P<ordinal>thu |so )?(?P<index>[1-9]\d*|nhat|mot|dau tien|dau|hai|nhi|ba|bon|tu|nam|cuoi|cuoi cung)"
    r"(?: nhe| nha| di| a)?"
)


def choice_index(value, count):
    """1-based option index for "chọn cái thứ nhất" / "số 2" / "cái cuối"."""
    match = CHOICE_PATTERN.fullmatch(value)
    if not match:
        return None
    word = match["index"]
    # A bare "Nam" or "Ba" is a place name; number words need "thứ"/"cái".
    if not word.isdigit() and not (match["ordinal"] or match["noun"]) and word not in {"dau tien", "cuoi cung"}:
        return None
    if word in {"cuoi", "cuoi cung"}:
        return count or None
    return int(word) if word.isdigit() else ORDINALS[word]


def literal_question_reply(text, session):
    """Recognize only a complete, short answer to the persisted question."""
    question = session.dialogue_question
    if question is None:
        return None
    raw, value = folded_reply(text)
    option = None
    # Match the whole answer, allowing spelling variants and polite endings.
    # Never strip a new command, correction or condition into an approval.
    affirmative = re.sub(r"(?:\s+(?:nhe|nha|a|di|luon))+$", "", value)
    if (
        affirmative in {"co", "dong y", "xac nhan", "u", "vang", "bat dau", "duoc", "duoc roi", "yes", "yep"}
        or re.fullmatch(r"(?:ok(?:e+|i+|ie+|a+y+)?|o ke|o kay)", affirmative)
        or re.fullmatch(r"đúng(?: rồi)?(?: (?:nhé|nha|ạ|đi|luôn))*", raw)
    ):
        option = "yes"
    elif value in DENY_VALUES:
        option = "no"
    else:
        index = choice_index(value, len(question.options)) if question.options else None
        if index and index <= len(question.options):
            option = question.options[index - 1].id
        elif text.strip() in {o.id for o in question.options}:
            option = text.strip()
    return QuestionReply(question_id=question.id, option_id=option) if option else None


class LimitedFallback:
    def interpret(self, text, session, *_args):
        raw = " ".join(unicodedata.normalize("NFC", text.casefold()).strip(" .,!?:;…").split())
        value = normalize_text(raw)
        question = session.dialogue_question
        if re.fullmatch(r"(?:huy|huy yeu cau|huy thao tac|thoi huy|bo yeu cau)", value) or literal_cancel(
            text, session
        ):
            return SemanticTurn(act="cancel", task_ref="active")
        # Removing Vietnamese accents collapses đúng / dừng / đừng. Only an
        # explicit stop phrase may bypass the current question.
        if (
            raw in {"dừng", "dừng lại", "tạm dừng", "dừng hành trình", "dung lai", "tam dung", "dung hanh trinh"}
            or raw == "dung"
            and not question
        ):
            return SemanticTurn(act="request", actions=[{"capability": "navigation.stop"}])
        if raw in {"dung", "dung roi"} and question:
            return SemanticTurn(
                act="reply",
                text="Bạn muốn dừng hành trình hay xác nhận câu hỏi hiện tại? Bạn dùng nút hoặc nói rõ lại nhé.",
            )
        reply = literal_question_reply(text, session)
        if reply:
            # Return the bound answer so all tiers use the same resolver and
            # preserve a question when its answer is rejected.
            return SemanticTurn(act="answer", question_answer=reply)
        if question and question.expires_at > datetime.now(UTC):
            index = choice_index(value, len(question.options))
            if index and index > len(question.options):
                return SemanticTurn(act="reply", text="Số này không có trong danh sách. Bạn chọn lại nhé.")
        return SemanticTurn(
            act="clarify",
            text="Mình chưa xử lý được yêu cầu này. Bạn nói lại yêu cầu hoặc dùng nút trên màn hình nhé.",
        )
