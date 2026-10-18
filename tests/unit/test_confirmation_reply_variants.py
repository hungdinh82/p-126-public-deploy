"""Whole-answer confirmation variants must remain bound to a pending question."""

from types import SimpleNamespace

import pytest

from src.vivi.dialogue.fallback import literal_question_reply


@pytest.mark.parametrize(
    "text", ["okee nhưng đừng mở", "ok mở cửa sau", "không đồng ý", "chưa xác nhận", "dung", "đừng", "oke nếu xe dừng"]
)
def test_approval_variants_never_swallow_a_correction_or_new_command(text):
    session = SimpleNamespace(dialogue_question=SimpleNamespace(id="question", options=[]))
    assert literal_question_reply(text, session) is None


@pytest.mark.parametrize(
    "text", ["okee", "okeee nhé", "oki", "okie", "ô kê", "o kê nha", "được rồi", "vâng ạ", "đúng rồi nhé", "yes"]
)
def test_whole_answer_approval_variants_require_a_pending_question(text):
    session = SimpleNamespace(dialogue_question=SimpleNamespace(id="question", options=[]))
    answer = literal_question_reply(text, session)
    assert answer.question_id == "question" and answer.option_id == "yes"
    assert literal_question_reply(text, SimpleNamespace(dialogue_question=None)) is None
