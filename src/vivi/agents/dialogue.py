"""Small dialogue-act policy; factual answers and vehicle actions live elsewhere."""

from __future__ import annotations

import re
import unicodedata

from src.vivi.agents.contracts import IntentDecision
from src.vivi.text import normalize_text


def clarify(question: str) -> IntentDecision:
    return IntentDecision(
        route="clarify", intent="conversation.clarify", needs_clarification=True, clarification_question=question
    )


def respond(text: str) -> IntentDecision:
    return IntentDecision(route="conversation", intent="conversation.respond", response_text=text)


def dialogue_decision(input_text: str) -> IntentDecision | None:
    raw = unicodedata.normalize("NFC", input_text).strip()
    text = normalize_text(raw).strip(" .?!")
    if re.search(r"\b(?:toi|minh)\b.*buon|chan qua", text) and "buon ngu" not in text:
        return IntentDecision(route="conversation", intent="conversation.respond",
                              response_text="Mình ở đây với bạn. Bạn muốn nghe nhạc một chút không?",
                              follow_up={"intent": "media.play"})
    if re.search(
        r"(?:ban|vivi).*(?:giup|ho tro|lam duoc|biet lam).*(?:gi|nhung gi)|(?:ban|vivi).*(?:kha nang|chuc nang)|giup duoc gi|chuc nang cua ban",
        text,
    ):
        return respond(
            "Mình có thể chỉnh điều hoà, cửa sổ, khoá cửa, sưởi ghế, bật nhạc, đọc tình trạng xe và giải thích cách dùng VF8."
        )
    intro = re.fullmatch(r"(?:toi|minh) ten (?:la )?([^,.!?]+)(?:[,!. ]+(?:rat )?vui.*)?[.!?]*", text)
    if intro and len(intro[1].split()) <= 5 and not re.search(r"\b(mo|dong|dat|tat|bat|khoa|giup|phat)\b", intro[1]):
        name = raw[intro.start(1) : intro.end(1)].strip()
        return respond(f"Rất vui được làm quen, {name}. Mình là ViVi, đồng hành cùng bạn trên chiếc VF8.")
    # A broad car description has no concrete retrieval target.
    if re.search(r"(?:biet gi|co thong tin gi|gioi thieu|tim hieu|muon hoi|can hoi).*\b(?:xe|vf\s*8)\b", text):
        from src.vivi.rag.knowledge import find_topic

        if find_topic(raw) is None:
            return clarify("Bạn muốn tìm hiểu về pin và sạc, các tiện ích trong xe hay hỗ trợ lái?")
    if re.fullmatch(r"(?:huong dan (?:su dung )?xe|(?:bo )?cam nang(?: co (?:gi|nhung thong tin gi))?)", text):
        return clarify("Bạn muốn tìm hiểu về sạc pin, tiện ích cabin hay hỗ trợ lái?")
    if re.fullmatch(r"(?:ve |hoi ve )?pin", text):
        return clarify("Bạn muốn kiểm tra mức pin hiện tại hay tìm hiểu dung lượng và cách sạc?")
    if re.search(r"\b(?:den|man hinh)\b.*(?:do|vang|bao|canh bao|cai gi)", text) and not re.search(
        r"phanh|pin|lop|dau|nhiet|day dai|tui khi|abs|srs|dong co|sac|den pha|den noi that|den suong mu", text
    ):
        return clarify("Bạn đọc giúp mình thông báo hoặc mô tả biểu tượng trên màn hình nhé.")
    if (
        "sac" in text
        and re.search(r"bao lau|bao nhieu (?:gio|phut)|thoi gian", text)
        and not re.search(r"\bac\b|\bdc\b|sac nhanh|sac cham|dien thoai|khong day|bao lau.*rut|rut.*bao lau", text)
    ):
        return clarify("Bạn muốn biết thời gian sạc AC hay sạc nhanh DC?")
    acts = (
        (
            r"^(?:khong co gi|thoi(?: bo qua)?|bo qua|khong can)(?: nhe| a| nua| di)?(?:,.*)?$",
            "Được nhé, mình vẫn ở đây khi bạn cần.",
        ),
        (r"ban la ai|vivi la ai|gioi thieu.*ban", "Mình là ViVi, tiếng nói của chiếc VF8 đang đồng hành cùng bạn."),
        (
            r"(?:xe nay|vf8|xe vf8|ban|vivi) la (?:loai )?xe gi",
            "Mình là ViVi trên chiếc VF8 của VinFast, đồng hành cùng bạn trong mỗi chuyến đi.",
        ),
        (r"tam biet|hen gap lai", "Hẹn gặp lại bạn nhé."),
        (r"ban (?:co )?khoe|vivi (?:co )?khoe", "Mình luôn sẵn sàng đồng hành cùng bạn nhé."),
        (r"\b(?:toi|minh)\b.*met|met qua|buon ngu", "Nếu bạn mệt, hãy tìm chỗ an toàn để nghỉ một chút nhé."),
        (r"sai roi|ban khong hieu|vivi khong hieu|(?:ban|vivi).*(?:ngu|te qua|do qua)", "Mình hiểu chưa đúng rồi. Bạn nói lại điều muốn mình làm nhé."),
        (r"alo|nghe (?:thay|ro)|co nghe", "Mình nghe rõ, bạn nói nhé."),
        (r"cam on", "Không có gì nhé."),
        (r"^(?:xin )?chao(?: (?:vivi|ban))?$|^vivi$|^hello$|^hi$", "Mình đây, bạn cần gì nhé?"),
    )
    for pattern, answer in acts:
        if re.search(pattern, text):
            return respond(answer)
    return None
