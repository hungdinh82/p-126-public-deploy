"""Whole-utterance recovery grammar; never interprets a new compound request."""

import re
import unicodedata
from datetime import UTC, datetime

from src.vivi.dialogue.contracts import QuestionReply, SemanticTurn
from src.vivi.text import normalize_text


class LimitedFallback:
    def interpret(self, text, session, *_args):
        from src.vivi.dialogue.answers import AnswerResolver

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
        if question and question.expires_at > datetime.now(UTC):
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
                if index:
                    ordinal = int(index[1])
                    if ordinal <= len(question.options):
                        option = question.options[ordinal - 1].id
                    else:
                        return SemanticTurn(act="clarify", text="Số này không có trong danh sách. Bạn chọn lại nhé.")
                elif text.strip() in {o.id for o in question.options}:
                    option = text.strip()
            if option:
                return AnswerResolver().resolve(
                    "", session, reply=QuestionReply(question_id=question.id, option_id=option)
                )
        return SemanticTurn(
            act="reply",
            text="Mình chưa kết nối được model để hiểu yêu cầu này. Yêu cầu đang chờ vẫn được giữ; bạn thử lại hoặc dùng nút trên màn hình nhé.",
        )
