"""Whole-utterance recovery grammar; never interprets a new compound request."""

import re
import unicodedata
from datetime import UTC, datetime

from src.vivi.dialogue.contracts import QuestionReply, SemanticTurn
from src.vivi.text import normalize_text


def literal_question_reply(text, session):
    """Recognize only a complete, short answer to the persisted question."""
    question = session.dialogue_question
    if question is None:
        return None
    raw = " ".join(unicodedata.normalize("NFC", text.casefold()).strip(" .,!?:;…").split())
    value = normalize_text(raw)
    option = None
    if value in {"co", "ok", "okay", "dong y", "xac nhan", "u", "vang", "bat dau"} or raw in {
        "đúng",
        "đúng rồi",
    }:
        option = "yes"
    elif value in {"khong", "chua", "khong dung", "de sau"}:
        option = "no"
    else:
        index = re.fullmatch(r"(?:chon |cho |dia diem |ket qua )?(?:thu |so )?([1-9]\d*)", value)
        if index and int(index[1]) <= len(question.options):
            option = question.options[int(index[1]) - 1].id
        elif text.strip() in {o.id for o in question.options}:
            option = text.strip()
    return QuestionReply(question_id=question.id, option_id=option) if option else None


class LimitedFallback:
    def interpret(self, text, session, *_args):
        raw = " ".join(unicodedata.normalize("NFC", text.casefold()).strip(" .,!?:;…").split())
        value = normalize_text(raw)
        question = session.dialogue_question
        if re.fullmatch(r"(?:huy|huy yeu cau|huy thao tac|thoi huy|bo yeu cau)", value):
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
            index = re.fullmatch(r"(?:chon |cho |dia diem |ket qua )?(?:thu |so )?([1-9]\d*)", value)
            if index and int(index[1]) > len(question.options):
                return SemanticTurn(act="reply", text="Số này không có trong danh sách. Bạn chọn lại nhé.")
        return SemanticTurn(
            act="clarify",
            text="Mình chưa xử lý được yêu cầu này. Bạn nói lại yêu cầu hoặc dùng nút trên màn hình nhé.",
        )
