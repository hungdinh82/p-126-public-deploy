"""Offline language boundary: lexer, bounded clause grammar and semantic frames.

All dialogue keyword matching lives here. It never writes memory or sends tools.
Unknown control clauses fail as a whole, rather than executing a known fragment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.vivi.capabilities.registry import DEFAULT_CLIMATE_DELTA_CELSIUS, compatible_change
from src.vivi.conversation.context import resolve_query, search_followup_query
from src.vivi.dialogue.contracts import ActionDraft, NavigationState, SemanticTurn, SessionState
from src.vivi.navigation.contracts import AreaConstraint, DestinationSpec
from src.vivi.navigation.destinations import (
    administrative_layer,
    landmark_for,
    region_for,
)
from src.vivi.text import normalize_text, strip_fillers


def place_query_matches(query, place):
    """POI evidence check; a nearby unrelated result is not a successful match."""
    parts = [part.strip() for part in query.split(",") if part.strip()]
    query = re.sub(r"\b(?:thanh pho|tp|tinh|phuong|xa)\s+", "", normalize_text(query))
    wanted = set(re.findall(r"[a-z0-9]+", query))
    name = set(re.findall(r"[a-z0-9]+", normalize_text(place.get("name", ""))))
    identity = re.split(r"\b(?:cong|loi vao|chi nhanh|co so|tang|toa|gate|building|tower)\b", normalize_text(parts[0]))[0] if parts else ""
    identity = re.sub(r"^(?:truong(?: (?:tieu hoc|trung hoc(?: co so| pho thong)?|dai hoc|mam non))?|ho|den|lang|ga|cong vien|quang truong|duong|pho|nha hang|khach san|quan (?:ca phe|cafe|pho))\s*\b", "", identity).strip()
    identity_words = set(re.findall(r"[a-z0-9]+", identity))
    if identity_words and not re.match(r"^\d", identity.strip()) and len(identity_words & name) / len(identity_words) < .75:
        # Shared area words must not make a different venue look like a match.
        return False
    evidence = normalize_text(
        " ".join(
            [
                place.get("name", ""),
                place.get("address", ""),
                *(b.get("full_name") or b.get("name", "") for b in place.get("boundaries", [])),
            ]
        )
    )
    available = set(re.findall(r"[a-z0-9]+", evidence))
    for parent in parts[1:]:
        region = region_for(parent)
        if region and not any(
            re.search(rf"\b{re.escape(normalize_text(name))}\b", evidence)
            for name in [region["name"], *region.get("aliases", [])]
        ):
            return False
    return bool(wanted and wanted.intersection(name) and len(wanted & available) / len(wanted) >= 0.75)


ZONES = ("driver", "front_passenger", "rear_left", "rear_right")
# A spoken yes to a pending question, as normalized text. STT writes "OK" the
# Vietnamese way ("Ô kê"), and people add a verb or a tail: "ừ mở đi", "ok luôn nhé".
# Bare "dung" is left out on purpose: it is "đúng" but also "dừng" (stop).
_YES = r"(?:toi )?xac nhan|co|dong y|duoc(?: roi)?|ok|oke|okay|okie|oki|o ke|vang|da|uh|u|um|dung roi|chac chan|tat nhien|nhat tri"
_TAIL = r"di|luon|nhe|nha|di nhe|luon nhe|luon di|lien|ngay"
def affirmed(value: str) -> bool:
    """A yes, once STT punctuation is gone: Soniox writes a lone "OK" as "Ok,."."""
    return bool(re.fullmatch(AFFIRM, re.sub(r"[\s,.!?;:…]+", " ", value).strip()))


AFFIRM = (
    rf"(?:{_YES})(?: (?:{_YES}))?(?: (?:mo|lam|xoa|dong|thuc hien|bat|chay))?(?: (?:{_TAIL}))?"
    rf"|(?:lam|thuc hien)(?: (?:{_TAIL}))?"
)
LEXICON = (
    (r"(?:dang |phia )?(?:sau va (?:dang |phia )?truoc|truoc va (?:dang |phia )?sau)", "all"),
    (r"(?:ben )?phai (?:ghe |phia |hang )?truoc", "front_passenger"),
    (r"(?:ben )?trai (?:ghe |phia |hang )?truoc", "driver"),
    (r"(?:ben )?phai (?:ghe |phia |hang )?sau", "rear_right"),
    (r"(?:ben )?trai (?:ghe |phia |hang )?sau", "rear_left"),
    (r"kinh lai", "window driver"),
    (r"cua tai", "door driver"),
    (r"cua phu", "door front_passenger"),
    (r"(?:phia )?(?:truoc|hang truoc)(?: ben)? phai|ben ghe phu|ben phu|ghe phu", "front_passenger"),
    (r"(?:phia )?(?:truoc|hang truoc)(?: ben)? trai|ben ghe lai|ben tai|ben lai|ghe lai|ghe tai|tai xe", "driver"),
    (r"(?:phia |hang )?sau(?: ben)? trai", "rear_left"),
    (r"(?:phia |hang )?sau(?: ben)? phai", "rear_right"),
    (r"tat ca|toan bo|ca bon|bon cua|4 cua|het cac|het(?= (?:cua|window|kinh)\b)", "all"),
    (r"cua kinh|cua so|kinh xe", "window"),
    (r"cua ra vao(?: xe)?|cua xe", "door"),
    (r"nap ca[ -]?po|ca[ -]?po|capo|capot|nap may|cop truoc", "hood"),
    (r"cua hau|cop sau|cop xe|cop", "trunk"),
    (r"ben day|ben do|phia do|phia day", "reference"),
    (r"mot nua|1 nua|phan nua", "50%"),
    (r"phan tram", "%"),
    (r"hai(?= do\b)", "2"),
)


@dataclass(frozen=True)
class Clause:
    text: str
    raw: str


class Lexer:
    def normalize(self, text):
        # STT may end a sentence cut at a pause with a comma.
        value = normalize_text(strip_fillers(text)).strip(" .!?;,:…")
        consent = re.fullmatch(
            r"(?:ok|oke|okay|vang|dong y|u)[, ]+(xac nhan|bat dau(?: di| hanh trinh| dan duong)?(?: ngay| luon)?)", value
        )
        if consent:
            value = consent[1]
        for pattern, replacement in LEXICON:
            value = re.sub(r"\b(?:" + pattern + r")\b", replacement, value)
        value = re.sub(r"\ball cac (?=window|door|cua|kinh)", "all ", value)
        value = re.sub(r"^(?:ban |vivi )?co the (?=(?:mo|dong|ha|khoa|phat|dat|chinh)\b)", "", value)
        value = (
            re.sub(r"(?: giup (?:toi|minh))? khong$", "", value)
            if re.match(r"^(?:mo|dong|ha|khoa|phat|dat|chinh)\b", value)
            else value
        )
        value = re.sub(r"^(?:vivi(?: oi)?[, ]+|hay |ban hay |giup toi |giup minh |lam on |co the )+", "", value)
        value = re.sub(r"^(?:xin )?chao(?: ban| vivi)?[, ]+(?=toi|minh|mo|dong|tang|giam|tim)", "", value)
        value = re.sub(r"^(?:toi|minh) (?:muon|can) (?=mo|dong|dat|tim|dan)\b", "", value)
        value = re.sub(r"(?:[, ]+(?:nhe|di|thoi|a|voi|luon|giup toi|giup minh|xuong))+$", "", value)
        value = re.sub(r"(?<=window) lay gio troi$", "", value)
        if re.match(r"^(?:mo|dong|ha|nang|khoa|phat|dat|chinh|tang|giam|bat|tat|tim|dan|chi duong)\b", value):
            value = re.sub(r" (?:cho toi|cho minh)$", "", value)
        return re.sub(r"\s+", " ", value).strip()

    def clauses(self, text):
        # Splitting before parsing requires every conjunct to produce a frame.
        raw = re.split(
            r"\s+(?:và|va|rồi|roi|sau đó|sau do)\s+(?=(?:mở|mo|đóng|dong|đặt|dat|chỉnh|chinh|tăng|tang|giảm|giam|hạ|ha|bật|bat|tắt|tat|phát|phat|dừng|dung|khoá|khóa|khoa|sưởi|suoi|đọc|doc|kiểm|kiem|gửi|gui|gọi|goi|xóa|xoá|xoa|quên|quen|ghi|nhớ|nho)\b)|[,;]\s*(?=(?:mở|mo|đóng|dong|đặt|dat|phát|phat|bật|bat|tắt|tat)\b)",
            text.strip(),
            flags=re.I,
        )
        raw = [
            part
            for clause in raw
            for part in re.split(
                r"\s+(?:và|va|rồi|roi|sau đó|sau do)\s+(?=(?:cho (?:tôi |toi |mình |minh )?biết (?:thời tiết|thoi tiet)|hỏi thời tiết|hoi thoi tiet|kể chuyện|ke chuyen|giới thiệu|gioi thieu|tìm hiểu|tim hieu|tra (?:trên |tren )?mạng|tra mang)\b)", clause, flags=re.I
            )
        ]
        raw = [
            part
            for clause in raw
            for part in re.split(
                r"\s+(?:và|va|rồi|roi)\s+(?=(?:thêm|them)\s+(?:bật|bat|tắt|tat|phát|phat|mở|mo)\b)", clause, flags=re.I
            )
        ]
        raw = [
            part
            for clause in raw
            for part in re.split(
                r"\s+(?:và|va|rồi|roi|sau đó|sau do)\s+(?=(?:tìm|tim|dẫn|dan|chỉ đường|chi duong)\b)|(?<=(?:đây|day))\s+(?=(?:đặt|dat|chỉnh|chinh)\s+(?:điều|dieu|nhiệt|nhiet)\b)",
                clause,
                flags=re.I,
            )
        ]
        return [Clause(self.normalize(part), part.strip()) for part in raw if part.strip()]


class OfflineParser:
    def __init__(self):
        self.lexer = Lexer()

    def interpret(self, text: str, session: SessionState, memory=None, history=None) -> SemanticTurn:
        text = strip_fillers(text)
        gear = re.fullmatch(
            r"(?:chuyen|ve|sang|vao)(?: sang| ve)?(?: so| che do)? ([pd])(?: nhe| di)?",
            normalize_text(text).strip(" .!?"),
        )
        if gear:
            return self.request("navigation.set_gear", {"gear": gear[1].upper()})
        # A comfort complaint followed by one aligned adjustment describes one
        # command, not two independent increments. Both clauses are consumed.
        comfort = re.fullmatch(
            r"(?:Tôi|Toi|Mình|Minh)\s+(?:hơi|hoi)\s+(lạnh|lanh|nóng|nong)\s*,\s*(.+)", text.strip(), re.I
        )
        if comfort:
            command = self.command(self.lexer.normalize(comfort[2]))
            if command and command.capability == "climate.set_temperature":
                delta = command.arguments.temperature_delta_celsius
                direction = 1 if normalize_text(comfort[1]) == "lanh" else -1
                if delta is not None and delta * direction > 0:
                    return SemanticTurn(act="request", actions=[command])
                return self.ask("Bạn nói rõ nhiệt độ muốn đặt nhé.")
        from src.vivi.dialogue.answers import AnswerResolver

        answer = AnswerResolver().resolve(text, session)
        if answer:
            return answer
        value = self.lexer.normalize(text)
        from src.vivi.dialogue.questions import current_question

        if affirmed(value) and current_question(session) is None:
            return self.ask("Bạn muốn xác nhận thao tác nào? Bạn nói rõ yêu cầu nhé.")
        # The lexer already drops the name "vivi", so a bare "ViVi ơi" arrives as "oi".
        if re.fullmatch(r"(?:(?:hey|hay|he|e|nay|ne|alo),? ?)?(?:v[iy] ?v[iy](?: ?(?:oi|a|nhe|nha))?|oi)", value):
            # Only the wake phrase reached STT: answer the call instead of "not understood".
            return SemanticTurn(act="reply", text="Mình đây, bạn cần gì nhé?")
        task = session.active
        index = task.current_index() if task else None
        current = task.steps[index] if index is not None else None
        live = bool(current)
        if live and len(self.lexer.clauses(text)) > 1 and affirmed(value):
            # "Ừ, mở đi" is one yes split by a comma, not two operations.
            return SemanticTurn(act="confirm", task_ref="active")
        dislike = re.search(r"\bkhong (?:thich|muon) (?:dung|su dung|bat) dieu hoa\b", value)
        if dislike:
            return SemanticTurn(
                act="reply",
                negated=True,
                text="Mình hiểu bạn không muốn dùng điều hoà. Bạn muốn mở cửa sổ ở vị trí nào?",
            )
        refusal = re.fullmatch(r"(?:khong|huy)(?:[, ]+)(.+)", value)
        replacement = self.command(refusal[1]) if refusal else None
        if (
            live
            and replacement
            and not replacement.referenced_target
            and not compatible_change(current.draft, replacement)
        ):
            # A refusal followed by a complete new operation replaces the
            # pending plan; it cannot approve any of the old steps.
            return self.request(replacement.capability, replacement.arguments.model_dump(exclude_none=True))
        if scoped := self.scoped_patch(value, session):
            return scoped
        if open_turn := self.open_world(text, value, session):
            return open_turn
        clarification = session.clarification
        if clarification and len(clarification.unresolved) == 1:
            replacement = re.sub(r"^(?:ý là|y la|à|a|đổi thành|doi thanh)\s+", "", text.strip(), flags=re.I)
            if self.command(self.lexer.normalize(replacement)):
                clauses = self.lexer.clauses(clarification.source_text)
                unknown = self.lexer.normalize(clarification.unresolved[0])
                updated = [replacement if clause.text == unknown else clause.raw for clause in clauses]
                if any(clause.text == unknown for clause in clauses):
                    return self.interpret(
                        " và ".join(updated), SessionState(navigation=session.navigation), memory, history
                    )
        if len(self.lexer.clauses(text)) == 1:
            if trip := self.trip(value, text, session):
                return trip
            if navigation := self.navigation(value, text, session):
                return navigation
        if live:
            cleaned = re.sub(r"^(?:khong[, ]+|doi (?:thanh|sang) |chi )", "", value)
            if patch := self.slot_patch(cleaned, current.draft, text):
                return SemanticTurn(act="modify", task_ref="active", actions=[patch])
        if not live and task:
            previous = next(
                (
                    s
                    for s in reversed(task.steps)
                    if s.draft.capability == "climate.set_temperature" and s.status == "completed"
                ),
                None,
            )
            patch = self.slot_patch(value, previous.draft, text) if previous else None
            if patch and (
                patch.arguments.temperature_delta_celsius is not None or patch.arguments.value_celsius is not None
            ):
                return self.request("climate.set_temperature", patch.arguments.model_dump(exclude_none=True))
        if affirmed(value):
            return (
                SemanticTurn(act="confirm", task_ref="active")
                if live
                else self.ask("Mình chưa có thao tác đang chờ xác nhận.")
            )
        if re.fullmatch(
            r"(?:khong|khong can|thoi|thoi khoi|bo qua|huy|huy yeu cau|dung lam|huy thao tac|khong mo nua)"
            r"(?: (?:dau|nhe|nha|nua|di))?",
            value,
        ):
            return (
                SemanticTurn(act="cancel", task_ref="active")
                if live
                else SemanticTurn(act="reply", text="Được nhé, mình bỏ qua yêu cầu đó.")
            )
        if re.search(r"^(?:de do da|tam dung yeu cau|tam giu|tam thoi dung)", value) and live:
            return SemanticTurn(act="suspend", task_ref="active", text="Mình tạm giữ yêu cầu đó.")
        if re.search(r"^(?:tiep tuc|quay lai|lam tiep)", value):
            return SemanticTurn(
                act="resume", task_ref="active" if live else "suspended:0" if session.suspended else None
            )
        # A complete noun/target grammar keeps "and" inside target sets and
        # exclusions. Other recognized control conjunctions become ordered
        # frames before any single-question/status/memory shortcut can fire.
        clauses = self.lexer.clauses(text)
        literal_memory = re.match(
            r"^(?:ghi nhớ|ghi nho|nhớ rằng|nho rang)\s+(?:lệnh|lenh|ghi chú|ghi chu)\s+.+?:", text.strip(), re.I
        )
        if not literal_memory and len(clauses) > 1 and self.command(value) is None:
            if len(clauses) > 5:
                return self.ask("Bạn chia yêu cầu thành tối đa năm thao tác mỗi lần nhé.")
            actions = []
            unknown = []
            correction = False
            patches, additions = [], []
            for clause in clauses:
                if clause.text in {"dan duong", "chi duong", "dan den do", "dan duong den do"} and any(
                    a.capability == "navigation.search" for a in actions
                ):
                    frame = self.request("navigation.plan", {"use_search_results": True})
                else:
                    clause_text = clause.raw
                    if actions and actions[-1].capability in {"navigation.plan", "trip.plan", "navigation.search", "web.search", "knowledge.lookup"}:
                        from src.vivi.conversation.context import topic_subject

                        previous = actions[-1]
                        query = previous.arguments.query or ""
                        subject = (DestinationSpec.from_arguments(previous.arguments.model_dump(exclude_none=True)).name
                                   if previous.capability.startswith(("navigation.", "trip.")) else topic_subject(query))
                        if subject:
                            scratch = SessionState(conversation_context={"capability": "knowledge.lookup", "subject": subject})
                            clause_text = resolve_query(clause_text, scratch, topic_reference=True)
                    frame = self.interpret(clause_text, session, memory, history)
                if (
                    frame.act not in {"request", "question", "modify"}
                    or frame.negated
                    or frame.conditional
                    or frame.scheduled
                    or frame.conflicting
                    or frame.unresolved
                    or not frame.drafts()
                ):
                    unknown.append(clause.raw)
                    continue
                actions.extend(frame.actions)
                patches.extend(frame.step_updates)
                additions.extend(frame.append_actions)
                correction |= frame.act == "modify"
            if len(actions) > 5:
                return self.ask("Bạn chia yêu cầu thành tối đa năm thao tác mỗi lần nhé.")
            if unknown:
                return SemanticTurn(
                    act="clarify",
                    clarification_drafts=actions,
                    unresolved=unknown,
                    text="Mình chưa hiểu đủ toàn bộ thao tác. Bạn nói rõ phần còn lại nhé.",
                )
            if patches or additions:
                if actions:
                    return self.ask("Bạn chỉ rõ các bước muốn sửa và các thao tác muốn thêm nhé.")
                return SemanticTurn(act="modify", task_ref="active", step_updates=patches, append_actions=additions)
            if live and correction:
                count = 0
                for incoming, existing in zip(actions, task.steps[index:]):
                    if existing.status in {"completed", "cancelled"} or not compatible_change(existing.draft, incoming):
                        break
                    count += 1
                if count:
                    return SemanticTurn(
                        act="modify",
                        task_ref="active",
                        step_index=index,
                        actions=actions[:count],
                        append_actions=actions[count:],
                    )
            return SemanticTurn(act="request", actions=actions)
        if memory_turn := self.memory(text, value, memory or []):
            return memory_turn
        if re.search(r"\b(?:neu|lat nua|ti nua|ngay mai|sau \d+ phut)\b", value):
            return SemanticTurn(
                act="clarify",
                conditional=True,
                text="Mình chưa thực hiện theo điều kiện hoặc hẹn giờ. Bạn nói thao tác cần làm ngay nhé.",
            )
        if re.match(r"^(?:dung|khong|thoi dung)\s+(?:mo|dong|phat|khoa|dat|ghi nho|luu)\b", value):
            return SemanticTurn(act="reply", negated=True, text="Được nhé, mình giữ nguyên.")
        if re.fullmatch(r"(?:xin )?chao(?: vivi| ban)?|vivi|hello|hi", value):
            name = next((item["value"] for item in memory or [] if item.get("key") == "display_name"), None)
            return SemanticTurn(
                act="reply", text=(f"Mình đây, {name}." if name else "Mình đây.") if live
                else f"Mình đây, {name}. Bạn cần gì thế?" if name else "Mình đây, bạn cần gì thế?"
            )
        if re.fullmatch(r"toi bao co sao ban khong mo nhac(?: luon)?", value):
            return self.request("media.play")
        if re.fullmatch(r"cam on(?: ban| vivi)?", value):
            return SemanticTurn(act="reply", text="Không có gì nhé.")
        if re.search(r"\b(?:buon|chan qua)\b", value) and "buon ngu" not in value:
            return SemanticTurn(
                act="offer",
                text="Mình ở đây với bạn. Bạn muốn nghe nhạc không?",
                actions=[ActionDraft(capability="media.play")],
            )
        if re.fullmatch(r"toi (?:bi )?dau bung", value):
            return SemanticTurn(
                act="offer",
                text="Bạn muốn mình tìm hiệu thuốc gần đây không?",
                actions=[
                    ActionDraft(
                        capability="navigation.search",
                        arguments={"query": "hiệu thuốc", "category": "pharmacy", "nearest": True},
                    )
                ],
            )
        if re.fullmatch(r"toi (?:thay |bi )?(?:hoi )?(?:om|cam sot|sot)", value):
            return SemanticTurn(act="reply", text="Mình nghe bạn. Bạn muốn mình tìm nơi hỗ trợ gần đây không?")
        from src.vivi.conversation.guardrails import assess_topic

        if assess_topic(text).handling == "caution" and re.search(
            r"\b(?:toi|minh|em) (?:dang )?(?:dau|met|bi benh|mac benh)\b", value
        ):
            return self.request("conversation.respond", {"query": text, "mode": "general"}, act="question")
        if re.search(r"quat gio", value) and re.match(r"^(?:tang|giam|bat|tat)", value):
            return SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ điều chỉnh tốc độ quạt gió nhé.")
        if re.search(r"\b(?:nong|lanh)\b", value) and re.search(r"toi da|cao nhat|max|toi thieu", value):
            return SemanticTurn(
                act="clarify",
                conflicting=True,
                text="Bạn muốn chỉnh nhiệt độ hay tốc độ quạt? Bạn nói rõ mức muốn đặt nhé.",
            )
        if re.fullmatch(r"trai|phai|sau|truoc", value):
            return self.ask("Bạn nói rõ vị trí cửa trước hay sau, bên trái hay phải nhé.")
        if live:
            cleaned = re.sub(r"^(?:khong[, ]+|doi (?:thanh|sang) |chi )", "", value)
            if patch := self.slot_patch(cleaned, current.draft):
                return SemanticTurn(act="modify", task_ref="active", actions=[patch])
        if re.search(
            r"(?:tat|bypass|hack|vo hieu hoa).*(?:tui khi|phanh|pin cao ap)|(?:thao|mo|hack) pin cao ap", value
        ):
            return SemanticTurn(
                act="unsupported", text="Mình không hướng dẫn can thiệp hệ thống an toàn hoặc điện cao áp."
            )
        if re.fullmatch(r"toi ten la .+", value):
            name = re.search(r"tôi tên là\s+([^,!.?]+)", text, re.I)
            return SemanticTurn(
                act="reply",
                text=f"Rất vui được làm quen với {name[1].strip() if name else 'bạn'}. Khi muốn lưu tên, bạn nói ghi nhớ nhé.",
            )
        if re.fullmatch(
            r"(?:ban la (?:xe gi|ai)|ban co khoe khong|toi (?:hoi )?met|ban khong hieu y toi|tam biet)", value
        ):
            return SemanticTurn(
                act="reply",
                text="Mình là ViVi, trợ lý trên xe. Mình ở đây nghe bạn; bạn có thể nói lại điều cần giúp nhé."
                if value != "tam biet"
                else "Tạm biệt, chúc bạn đi đường thuận lợi nhé.",
            )
        if re.fullmatch(r"toi thich .+", value):
            return SemanticTurn(act="reply", text="Mình nghe bạn rồi. Khi muốn lưu thông tin, bạn nói ghi nhớ nhé.")
        if re.fullmatch(r"khong co gi[, ]+ban chay tiep|thoi bo qua", value):
            return SemanticTurn(act="reply", text="Được nhé, mình bỏ qua.")
        if re.fullmatch(
            r"(?:nhung ma )?(?:ban |vivi )?(?:giup gi duoc cho (?:toi|minh)|co the lam gi|giup duoc nhung gi(?: da)?)",
            value,
        ):
            return SemanticTurn(
                act="reply",
                text="Mình có thể chỉnh điều hoà, điều khiển cửa sổ, phát nhạc, đọc tình trạng xe, tra cẩm nang và ghi nhớ thông tin khi bạn yêu cầu.",
            )
        if re.fullmatch(
            r"huong dan su dung xe|bo cam nang co nhung thong tin gi|toi muon hoi ve (?:chiec )?xe nay|ban biet gi ve (?:chiec )?xe nay",
            value,
        ):
            return self.ask("Bạn muốn tìm hiểu mục nào: pin, sạc, tính năng hay cách sử dụng xe?")
        if re.search(r"den.*(?:bao|canh bao).*(?:do|vang|mau)", value) and not re.search(
            r"phanh|pin|lop|tui khi", value
        ):
            return self.ask("Bạn mô tả biểu tượng của đèn cảnh báo đó giúp mình nhé.")
        if re.fullmatch(r"(?:bat|tat) (?:cruise control|dieu hoa)|dap phanh", value):
            return SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ điều khiển chức năng này bằng giọng nói.")
        if re.search(r"sac.*(?:bao lau|thoi gian)|(?:bao lau|thoi gian).*sac", value):
            mode = (
                "DC"
                if re.search(r"\bdc\b|sac nhanh", value)
                else "AC"
                if re.search(r"\bac\b|sac cham", value)
                else None
            )
            return self.request(
                "manual.search",
                {"query": text, "question_kind": "charging_duration", "charging_mode": mode},
                act="question",
            )
        if re.search(r"(?:window|door|hood|trunk|cua).*\b(?:khong .+ duoc|bi ket|hong)\b", value):
            return self.request("manual.search", {"query": text}, act="question")
        if self.status_fields(value):
            return self.request("vehicle.get_status", {"query": ",".join(self.status_fields(value))})
        # Explicitly naming cabin doors AND both panels is one supported body action.
        value = re.sub(r"\ball cac (?:door|cua)\b", "all door", value)
        if re.fullmatch(r"(?:mo|dong) all (?:door|cua)[, ]+(?:hood[, ]+(?:va )?trunk|trunk[, ]+(?:va )?hood)", value):
            return self.request("body.set_open", {"open": value.startswith("mo")})
        # Unknown imperative clauses cannot masquerade as knowledge questions
        # merely because they contain a noun such as "tính năng".
        if (
            re.match(r"^(?:mo|dong|dat|chinh|tang|giam|ha|bat|tat|phat|khoa|kich hoat)\b", value)
            and self.command(value) is None
            and not re.search(r"\b(?:cach|huong dan|lam sao|the nao|nhu nao|dung nut|nut nao|ra sao|xu ly)\b", value)
        ):
            return self.ask("Mình chưa hiểu đủ thao tác này. Bạn nói rõ yêu cầu nhé.")
        # Knowledge questions take precedence over imperatives inside a question.
        if re.search(
            r"\b(?:cach|huong dan|lam sao|tai sao|vi sao|la gi|o dau|bao lau|bao nhieu|thong tin ve|thong so|tinh nang|co nhung gi|co.*khong|the nao|nhu nao)\b|^thay\b|^nhiet do (?:cua xe|door)\b",
            value,
        ):
            if re.search(
                r"(?:phanh|pin cao ap|tui khi).*(?:bypass|hack)|(?:bypass|hack|vo hieu hoa|thao pin|mo pin)", value
            ):
                return SemanticTurn(
                    act="unsupported", text="Mình không hướng dẫn can thiệp hệ thống an toàn hoặc điện cao áp."
                )
            # Explicit measurement cues distinguish observations from handbook facts.
            from src.vivi.rag.knowledge import find_topic

            referenced = self.question_reference(text, history or [])
            # Use the handbook vocabulary before the command lexer replaces
            # Vietnamese names such as "cửa sổ" with control tokens.
            vehicle_topic = find_topic(referenced) or re.search(r"\b(?:xe|vf\d+|vinfast|pin|sac|lop|phanh|cua|kinh|dieu hoa|cabin|cruise|lka|adas|tui khi|capo|cop|vo lang|dong co|den|ghe|cam bien|dau|nuoc lam mat|ho tro lai)\b", normalize_text(referenced))
            if not vehicle_topic:
                return self.request("conversation.respond", {"query": text, "mode": "general"}, act="question")
            if re.search(r"hien tai|bay gio|con bao nhieu|con di duoc", value) and not re.search(
                r"cach|huong dan|tai sao", value
            ):
                return self.request("vehicle.get_status")
            return self.request(
                "manual.search", {"query": self.question_reference(text, history or [])}, act="question"
            )
        if re.search(
            r"\b(?:trang thai xe|tinh trang xe|kiem tra pin|pin con|ap suat lop hien tai|xe con di|kiem tra xe)\b",
            value,
        ):
            return self.request("vehicle.get_status")
        if re.search(r"gia ban|khuyen mai", value):
            return self.request("web.search", {"query": text}, act="question")
        if text.rstrip().endswith("?") and self.command(value) is None:
            from src.vivi.rag.knowledge import find_topic

            # Nominal knowledge questions still support edition/spec lookups.
            # Movement verbs make an unrecognized phrase ambiguous, not RAG.
            if find_topic(text) and not re.search(r"\b(?:đến|tới|ghé|đưa|chở|đi|về)\b", text, re.I):
                return self.request("manual.search", {"query": text}, act="question")
        clauses = self.lexer.clauses(text)
        if len(clauses) > 5:
            return self.ask("Bạn chia yêu cầu thành tối đa năm thao tác mỗi lần nhé.")
        actions = []
        for clause in clauses:
            parsed = self.command(clause.text)
            if parsed is None:
                if len(clauses) > 1 or re.search(r"\b(?:mo|dong|dat|khoa|tang|giam|bat|tat|phat|ha)\b", clause.text):
                    return self.ask("Mình chưa hiểu đủ toàn bộ thao tác. Bạn nói rõ các bước muốn thực hiện nhé.")
                return self.ask("Mình chưa hiểu yêu cầu. Bạn nói rõ thao tác hoặc điều muốn hỏi về xe nhé.")
            if parsed.referenced_target:
                if current is None:
                    return self.ask("Bạn muốn thực hiện ở vị trí nào?")
                targets = current.draft.arguments.model_dump(exclude_none=True)
                for key in ("zone", "zones"):
                    if key in targets:
                        setattr(parsed.arguments, key, targets[key])
            actions.append(parsed)
        if live and len(actions) == 1 and (value.startswith(("khong", "chi", "doi")) or actions[0].referenced_target):
            return SemanticTurn(act="modify", task_ref="active", actions=actions)
        return SemanticTurn(act="request", actions=actions)

    def open_world(self, text, value, session):
        """Bounded open-world grammar, before navigation and scheduled effects."""
        # Preserve commands, memory writes and consent ownership.
        if self.command(value) or re.match(r"^(?:ghi nho|nho rang|quen|mo|dong|dat|chinh|bat|tat|phat|khoa|dan(?! so\b)|dua|di den|di toi|bat dau)\b", value):
            return None
        if re.search(r"\b(?:neu|lat nua|sau do|thi)\b.*\b(?:mo|dong|dat|bat|tat|phat|khoa|ha|chinh)\b", value):
            return None
        current = session.active.steps[session.active.current_index()] if session.active and session.active.current_index() is not None else None
        from src.vivi.information.policy import information_decision

        resolved_text = resolve_query(text, session, topic_reference=True)
        decision = information_decision(resolved_text)
        if decision and decision.capability != "weather.get_forecast" and len(self.lexer.clauses(text)) == 1:
            return self.request(decision.capability, {"query": resolved_text}, act="question")
        if (session.conversation_context.get("focus") == "information" and affirmed(value)
            and (current is None or current.draft.capability in {"conversation.respond", "web.search", "weather.get_forecast", "knowledge.lookup"})):
            return SemanticTurn(act="clarify", text="Bạn nói rõ yêu cầu muốn tiếp tục nhé; mình chưa xác nhận thao tác nào.", unresolved=[text])
        if (current and current.draft.capability == "weather.get_forecast" and current.status == "waiting_slot"
            and re.fullmatch(r"[\w\s,.-]{2,100}", text, re.UNICODE)
            and len(text.split()) <= 7 and not re.search(r"\b(?:khong|co|huy|thoi|cam on|chao|ke|tai sao|vi sao|la gi|ngay mai|ngay kia|lich su|van hoa|du lich|noi chuyen|tam su|met|lo trinh|ke hoach|thoi tiet|du bao)\b", value)):
            args = current.draft.arguments.model_dump(exclude_none=True)
            location = self.weather_location(text, slot_reply=True)
            if location:
                args.update(location=location, query=current.draft.arguments.query or "Thời tiết")
                return SemanticTurn(act="modify", task_ref="active", actions=[ActionDraft(capability="weather.get_forecast", arguments=args)])
        # Let the existing ordered-clause parser preserve mixed command chains.
        if len(self.lexer.clauses(text)) > 1:
            return None
        from src.vivi.conversation.guardrails import assess_topic

        topic = assess_topic(text)
        inquiry = text.rstrip().endswith("?") or re.search(
            r"\b(?:la gi|tai sao|vi sao|the nao|nhu nao|tac dung|rui ro|bao nhieu|co.*khong)\b", value
        )
        if topic.handling == "caution" and not topic.terminal and inquiry:
            # Health words such as "đau" must not become the oil/vehicle topic.
            # Explicit current facts have already selected their lookup above.
            return self.request("conversation.respond", {"query": text, "mode": "general"}, act="question")
        if re.search(r"\b(?:ke (?:cho .+ )?(?:chuyen|truyen)|ke tiep|truyen ngan|chuyen vui)\b", value):
            return self.request("conversation.respond", {"query": text, "mode": "creative"}, act="question")
        previous = session.conversation_context
        date_followup = re.fullmatch(r"(?:con |the )?(?:ngay mai|ngay kia)(?: thi sao)?[?.! ]*", value)
        location_followup = (previous.get("capability") == "weather.get_forecast"
            and (region_for(text.strip(" .!?")) or re.match(r"^(?:(?:minh|toi)(?: dang)? )?(?:o|tai)\s+", value))
            and not self.command(value))
        if location_followup:
            location = self.weather_location(text, slot_reply=True)
            if location:
                return self.request("weather.get_forecast", {"query": "Thời tiết tại " + location, "location": location,
                    "forecast_day": previous.get("forecast_day", "today")}, act="question")
        if re.search(r"\b(?:thoi tiet|du bao|troi.*(?:mua|nang)|co mua|mua khong)\b", value) or (date_followup and previous.get("capability") == "weather.get_forecast"):
            query = resolve_query(text, session)
            location = self.weather_location(query)
            day = "day_after_tomorrow" if "ngay kia" in value else "tomorrow" if "ngay mai" in value else "today"
            args = {"query": text, "forecast_day": day}
            if location:
                args["location"] = location
            return self.request("weather.get_forecast", args, act="question")
        if self.search_followup(value, session):
            return self.request("web.search", {"query": search_followup_query(text, session)}, act="question")
        if re.search(r"\b(?:am lich|hom nay.*bao nhieu am)\b", value):
            return self.request("web.search", {"query": text}, act="question")
        if re.search(r"\b(?:tim (?:tren )?mang|tra (?:tren )?mang|internet|google|tim kiem tren|tin moi|moi nhat|gia vang|du lich|tham quan|gi choi|choi gi|gi vui|su kien|cho nao dang di)\b", value):
            return self.request("web.search", {"query": resolve_query(text, session)}, act="question")
        if re.search(r"\b(?:wikipedia|lich su|van hoa|nguon goc)\b", value) and not re.search(r"\b(?:xe|vf\d+|vinfast|pin|sac)\b", value):
            query = re.sub(r"^(?:tìm hiểu|tra|tìm|cho (?:tôi|mình) biết)\s+", "", resolve_query(text, session), flags=re.I)
            return self.request("knowledge.lookup", {"query": query}, act="question")
        if re.search(r"\b(?:noi chuyen|tam su|hom nay.*met|toi met qua|minh met qua)\b", value):
            return self.request("conversation.respond", {"query": text, "mode": "chat"}, act="question")
        return None

    @staticmethod
    def search_followup(value, session):
        previous = session.conversation_context
        return bool(
            previous.get("capability") == "web.search"
            and previous.get("query")
            and re.search(
                r"^(?:(?:con|the con) (?:buoi toi|ban dem|an uong|cho an|di voi tre em|dia diem nao khac)\b"
                r"|(?:o|tai) do (?:co (?:gi|cho|dia diem|quan|mon|su kien)|an|choi|buoi toi)\b"
                r"|(?:co )?(?:cho|dia diem) nao (?:phu hop|danh cho) (?:tre em|gia dinh)\b)",
                value,
            )
            or previous.get("capability") in {"web.search", "knowledge.lookup"} and previous.get("query") and re.fullmatch(
                r"(?:cho|noi|neu)(?: (?:toi|minh))?(?: mot)? (?:con so|muc gia)(?: (?:cu the|di|nhe))*[.!? ]*", value
            )
        )

    @staticmethod
    def weather_location(text, *, slot_reply=False):
        """Extract a place span, excluding the speaker and question/date tails."""
        raw = text.strip(" ?.!,;:")
        value = normalize_text(raw)
        match = re.search(r"\b(?:o|tai)\s+(.+)", value)
        if match:
            raw = raw[match.start(1):]
        elif not slot_reply:
            match = re.search(r"\b(?:du bao )?thoi tiet\s+(.+)", value)
            if not match:
                return None
            raw = raw[match.start(1):]
        value = normalize_text(raw)
        leading = re.match(r"(?:(?:hom nay|ngay mai|ngay kia)\s+)+", value)
        if leading:
            raw = raw[leading.end():]
        value = normalize_text(raw)
        tail = re.search(r"\b(?:hom nay|ngay mai|ngay kia|chieu nay|chieu mai|co mua|co nang|mua khong|nang khong|the nao|nhu (?:the nao|nao)|ra sao|nhi|nhe)\b", value)
        if tail:
            raw = raw[:tail.start()]
        raw = raw.strip(" ?.!,;:")
        if normalize_text(raw) in {"", "day", "hien tai", "bay gio", "minh", "toi"}:
            return None
        return raw

    @staticmethod
    def ask(text):
        return SemanticTurn(act="clarify", text=text)

    def scoped_patch(self, value, session):
        task = session.active
        if not task or task.current_index() is None:
            return None
        if added := re.fullmatch(r"them (.+?) (?:vao |o )?cuoi(?: chuoi| yeu cau)?", value):
            if action := self.command(added[1]):
                return SemanticTurn(act="modify", task_ref="active", append_actions=[action])
        if edit := re.fullmatch(r"buoc (nhiet do|dieu hoa|nhac|[1-5]) (?:doi sang|doi thanh|dat|chinh) (.+)", value):
            if edit[1].isdigit():
                indices = [int(edit[1]) - 1]
            else:
                capability = "climate.set_temperature" if edit[1] in {"nhiet do", "dieu hoa"} else "media.play"
                indices = [
                    index
                    for index, step in enumerate(task.steps)
                    if step.draft.capability == capability
                    and step.status not in {"completed", "cancelled", "unknown", "running"}
                ]
            if len(indices) != 1 or indices[0] >= len(task.steps):
                return self.ask("Bạn muốn sửa bước số mấy trong chuỗi?")
            index = indices[0]
            if action := self.slot_patch(edit[2], task.steps[index].draft):
                return SemanticTurn(
                    act="modify", task_ref="active", step_updates=[{"step_index": index, "action": action}]
                )
        return None

    @staticmethod
    def request(capability, arguments=None, act="request"):
        arguments = dict(arguments or {})
        if capability == "manual.search" and arguments.get("query"):
            query = normalize_text(arguments["query"])
            if model := re.search(r"\bvf\d+\b", query):
                arguments["requested_vehicle_model"] = model[0].upper()
            if year := re.search(r"\b(?:nam|doi|mau|phien ban|vf\d+)\s+(20\d{2})\b", query):
                arguments["requested_model_year"] = int(year[1])
        return SemanticTurn(act=act, actions=[ActionDraft(capability=capability, arguments=arguments or {})])

    def slot_patch(self, value, draft, raw=None):
        # Correction particles belong to this contextual grammar, not global
        # normalization (where they could be part of a place or memory value).
        value = re.sub(r" (?:co ma|ay)$", "", value)
        value = re.sub(r"^(?:a thoi|a|thoi) ", "", value)
        if draft.capability in {"window.set_position", "door.set_open", "door.set_lock"}:
            pair = re.fullmatch(r"(?:chi )?(?:2|hai) (?:cua |window |door |kinh )?(truoc|sau)", value)
            if pair:
                return ActionDraft(
                    capability=draft.capability,
                    arguments={"zones": list(ZONES[:2] if pair[1] == "truoc" else ZONES[2:])},
                )
        if repair := re.fullmatch(r"((?:(?:ben|phia) )?(?:truoc|sau|trai|phai)) a khong (.+)", value):
            value = repair[2]
        if draft.capability in {"door.set_open", "door.set_lock", "window.set_position", "seat.set_heat_level"}:
            exclusion = re.fullmatch(
                r"(?:tru|ngoai (?:tru|tu))(?: tiep)?(?: ca)? (?:(?:cua|window|door|kinh|ben|phia|ghe) )*(driver|front_passenger|rear_left|rear_right)(?: ra)?",
                value,
            )
            if exclusion:
                return ActionDraft(
                    capability=draft.capability,
                    excluded_zones=[exclusion[1]],
                    exclusion_mode="replace" if self.lexer.normalize(raw or "").startswith("chi tru ") else "add",
                )
        noun_target = re.fullmatch(
            r"(window|kinh|door|cua)(?: ben| phia| o)? (driver|front_passenger|rear_left|rear_right|all)", value
        )
        if noun_target and draft.capability in {"door.set_open", "window.set_position"}:
            return ActionDraft(
                capability="window.set_position" if noun_target[1] in {"window", "kinh"} else "door.set_open",
                arguments={"zone": noun_target[2]},
            )
        if draft.capability == "memory.remember" and draft.arguments.memory_key == "display_name":
            if re.fullmatch(r"(?:luu|nho|ghi nho) (?:ten )?(?:toi|minh)", value):
                return ActionDraft(capability=draft.capability, arguments={"memory_key": "display_name"})
            if (
                value not in {"toi", "minh", "ban", "ten toi", "nhac"}
                and re.fullmatch(r"(?:ten (?:toi|minh) la |toi ten la )?[\w -]{1,70}", value)
                and not re.search(r"\b(?:mo|dong|nho|luu|ghi|tang|giam|huy|khong|ok|xac nhan)\b", value)
            ):
                name = re.sub(r"^(?:tên (?:tôi|mình) là |tôi tên là )", "", raw or value, flags=re.I)
                return ActionDraft(capability=draft.capability, arguments={"memory_value": name.strip()})
        if draft.capability == "climate.set_temperature":
            delegated = re.fullmatch(
                r"(tang|giam|ha)(?: tiep| them| len| xuong)?(?: mot chut| bao nhieu)?(?: cung duoc| tuy ban)?", value
            )
            if delegated and (
                any(word in value for word in ("tiep", "chut", "tuy ban", "cung duoc"))
                or value in {"tang", "tang len", "giam", "giam xuong", "ha"}
            ):
                return ActionDraft(
                    capability=draft.capability,
                    arguments={
                        "temperature_delta_celsius": (-1 if delegated[1] in {"giam", "ha"} else 1)
                        * DEFAULT_CLIMATE_DELTA_CELSIUS
                    },
                )
            if command := self.command(value):
                if command.capability == draft.capability:
                    return command
        if draft.capability in {"door.set_open", "door.set_lock", "window.set_position", "seat.set_heat_level"}:
            partial = re.fullmatch(
                r"(?:(?:cua |window |kinh |door |ben |phia |ghe )+)?(trai|phai|sau|truoc)(?: (trai|phai|sau|truoc))?",
                value,
            )
            if partial:
                args = {}
                for token in partial.groups():
                    if token in {"trai", "phai"}:
                        args["target_side"] = "left" if token == "trai" else "right"
                    elif token:
                        args["target_row"] = "rear" if token == "sau" else "front"
                capability = (
                    "window.set_position"
                    if re.match(r"^(?:window|kinh)\b", value)
                    else "door.set_open"
                    if re.match(r"^(?:door|cua)\b", value) and draft.capability == "window.set_position"
                    else draft.capability
                )
                return ActionDraft(capability=capability, arguments=args)
            if re.fullmatch(r"window|kinh|cua window", value):
                return ActionDraft(capability="window.set_position")
        if draft.capability == "manual.search" and re.fullmatch(r"(?:sac )?(?:ac|dc|sac nhanh|sac cham)", value):
            return ActionDraft(
                capability="manual.search",
                arguments={"charging_mode": "DC" if value in {"dc", "sac dc", "sac nhanh"} else "AC"},
            )
        if draft.capability in {
            "door.set_open",
            "door.set_lock",
            "window.set_position",
            "seat.set_heat_level",
        } and re.fullmatch(r"(?:o |phia |ben |cua |ghe )?(?:driver|front_passenger|rear_left|rear_right|all)", value):
            zone = value.split()[-1]
            return ActionDraft(capability=draft.capability, arguments={"zone": zone})
        if re.fullmatch(r"\d+(?:[.,]\d+)?(?: do(?: c)?)?", value) and draft.capability == "climate.set_temperature":
            return ActionDraft(
                capability=draft.capability, arguments={"value_celsius": float(value.split()[0].replace(",", "."))}
            )
        if re.fullmatch(r"(?:mo )?50%", value) and draft.capability == "window.set_position":
            return ActionDraft(capability=draft.capability, arguments={"position_percent": 50})
        if re.fullmatch(r"(?:cua ra vao(?: xe)?|door|cua)", value) and draft.capability in {
            "body.set_open",
            "door.set_open",
        }:
            return ActionDraft(capability="door.set_open", arguments={"zone": "all", "open": draft.arguments.open})
        if value.startswith(("tru ", "all tru ")):
            zone = re.sub(r"^(?:all )?tru (?:cua |door |window |ben )*", "", value)
            if zone in ZONES:
                return ActionDraft(
                    capability=draft.capability,
                    arguments={"zone": "all"} if value.startswith("all ") else {},
                    excluded_zones=[zone],
                )
        if re.fullmatch(r"(?:muc )?[0-3]", value) and draft.capability == "seat.set_heat_level":
            return ActionDraft(capability=draft.capability, arguments={"level": int(value[-1])})
        if command := self.command(value):
            if command.referenced_target or value.startswith(("khong", "chi", "doi")):
                return command
        return None

    def command(self, value):
        value = re.sub(r"^(?:khong[, ]+|doi (?:thanh|sang) )", "", value)
        exclusions = []
        if " tru " in value:
            value, excluded = value.split(" tru ", 1)
            excluded = re.sub(r"^(?:cua |window |door |ben )+", "", excluded)
            exclusions = [
                re.sub(r"^(?:cua |window |door |ben )+", "", item.strip())
                for item in re.split(r"\s+va\s+|,\s*", excluded)
            ]
            if (
                not exclusions
                or any(zone not in ZONES for zone in exclusions)
                or len(set(exclusions)) != len(exclusions)
            ):
                return None
        # A bounded noun/target/value grammar: every token is consumed.
        match = re.fullmatch(
            r"(mo|dong|ha|nang|len|khoa|mo khoa|bat|tat)\s+(?:(all)\s+)?(window|kinh|door|cua|hood|trunk)(?:\s+(.*))?",
            value,
        )
        if match:
            verb, all_target, noun, suffix = match.groups()
            suffix = suffix or ""
            percent = re.search(r"(?:^|\s)(\d+)\s*%(?:$|\s)", suffix)
            if percent:
                suffix = (suffix[: percent.start()] + " " + suffix[percent.end() :]).strip()
                suffix = re.sub(r"(?:\s+|^)(?:xuong|len|toi|o muc)$", "", suffix).strip()
            suffix = re.sub(r"^(?:o |ben |phia )+", "", suffix)
            suffix = re.sub(r"\s+(?:o |ben |phia )", " ", suffix)
            targets = [re.sub(r"^(?:ben |phia )+", "", token.strip()) for token in re.split(r"\s+va\s+|,\s*", suffix)]
            zones = (
                targets
                if len(targets) > 1 and all(target in ZONES for target in targets) and len(set(targets)) == len(targets)
                else None
            )
            zone = "all" if all_target else suffix if suffix in (*ZONES, "all") else None
            reference = suffix == "reference"
            if suffix in {"phia sau", "sau", "dang sau"} and noun in {"door", "cua"}:
                suffix = ""
            partial = re.fullmatch(r"(?:(?:dang |hang )?)(trai|phai|truoc|sau)", suffix)
            if suffix and zone is None and zones is None and not reference and not partial:
                return None
            args = {"zone": zone} if zone else {"zones": zones} if zones else {}
            if partial:
                args["target_side" if partial[1] in {"trai", "phai"} else "target_row"] = {
                    "trai": "left",
                    "phai": "right",
                    "truoc": "front",
                    "sau": "rear",
                }[partial[1]]
            if noun in {"window", "kinh"}:
                if verb not in {"mo", "dong", "ha", "nang", "len"}:
                    return None
                args["position_percent"] = int(percent[1]) if percent else 0 if verb in {"dong", "len"} else 100
                capability = "window.set_position"
            elif noun in {"door", "cua"}:
                if percent:
                    return None
                capability = "door.set_lock" if verb in {"khoa", "mo khoa"} else "door.set_open"
                args["locked" if capability == "door.set_lock" else "open"] = verb not in {"dong", "tat", "mo khoa"}
            else:
                if suffix or all_target or percent:
                    return None
                capability, args = (
                    ("hood.set_open" if noun == "hood" else "trunk.set_open"),
                    {"open": verb in {"mo", "bat", "nang"}},
                )
            return ActionDraft(
                capability=capability, arguments=args, excluded_zones=exclusions, referenced_target=reference
            )
        limit = re.fullmatch(
            r"(tang|giam|ha|dat|chinh) (?:dieu hoa|nhiet do)(?: len| xuong| ve)? (toi da|cao nhat|toi thieu|thap nhat)",
            value,
        )
        if limit:
            return ActionDraft(
                capability="climate.set_temperature",
                arguments={
                    "value_celsius": 16
                    if limit[1] in {"giam", "ha"}
                    else 30
                    if limit[2] in {"toi da", "cao nhat"}
                    else 16
                },
            )
        relative = re.fullmatch(
            r"(?:tang|giam|ha) (?:dieu hoa|nhiet do)(?: (them|bot|len|xuong))? (\d+(?:[.,]\d+)?) do(?: c)?", value
        )
        if relative:
            amount = float(relative[2].replace(",", "."))
            # A named target in the supported range differs from an explicit
            # increment (thêm/bớt). The model may use wider context; the guard
            # below does not force this range-based offline interpretation.
            if relative[1] in {"len", "xuong"} and 16 <= amount <= 30:
                return ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": amount})
            return ActionDraft(
                capability="climate.set_temperature",
                arguments={"temperature_delta_celsius": amount * (-1 if value.startswith(("giam", "ha")) else 1)},
            )
        match = re.fullmatch(
            r"(?:dat|chinh|tang(?: len)?|giam(?: xuong)?|ha)\s+(?:nhiet do|dieu hoa)(?:\s+(?:o|ve|den|len|xuong|thanh))?(?:\s+(\d+(?:[.,]\d+)?)(?: do(?: c)?)?)?",
            value,
        )
        if match:
            return ActionDraft(
                capability="climate.set_temperature",
                arguments={"value_celsius": float(match[1].replace(",", "."))}
                if match[1]
                else {
                    "temperature_delta_celsius": (-1 if value.startswith(("giam", "ha")) else 1)
                    * DEFAULT_CLIMATE_DELTA_CELSIUS
                }
                if value.startswith(("tang", "giam", "ha"))
                else {},
            )
        match = re.fullmatch(r"(?:tang|giam|ha)(?: them| bot| len| xuong)?\s+(\d+(?:[.,]\d+)?) do(?: c)?", value)
        if match:
            return ActionDraft(
                capability="climate.set_temperature",
                arguments={
                    "temperature_delta_celsius": float(match[1].replace(",", "."))
                    * (-1 if value.startswith(("giam", "ha")) else 1)
                },
            )
        if re.fullmatch(r"(?:toi |minh |hoi |qua |van )*(?:lanh|nong)(?: qua| lam)?|(?:mat|am) hon", value):
            return ActionDraft(
                capability="climate.set_temperature",
                arguments={"temperature_delta_celsius": -2 if "nong" in value or "mat" in value else 2},
            )
        match = re.fullmatch(
            r"(?:bat |tat |chinh )?(?:suoi ghe|ghe suoi|lam am ghe)(?:\s+(all|driver|front_passenger|rear_left|rear_right))?(?:\s+(?:muc|cap)\s+([0-3]))?",
            value,
        )
        if match:
            return ActionDraft(
                capability="seat.set_heat_level",
                arguments={
                    **({"zone": match[1]} if match[1] else {}),
                    "level": 0 if value.startswith("tat") else int(match[2] or 1),
                },
            )
        if re.fullmatch(r"(?:phat|mo|bat) nhac(?: len)?", value):
            return ActionDraft(capability="media.play")
        media = re.fullmatch(r"(?:phat|mo|bat) nhac (.+)", value)
        if media and not re.search(r"\b(?:mo|dong|tat|khoa|dat|va|roi)\b", media[1]):
            return ActionDraft(capability="media.play", arguments={"media_query": media[1]})
        if re.fullmatch(r"(?:dung|tat) nhac", value):
            return ActionDraft(capability="media.pause")
        if re.fullmatch(r"(?:kiem tra|doc|cho biet) (?:trang thai|tinh trang)(?: xe)?", value):
            return ActionDraft(capability="vehicle.get_status")
        return None

    def memory(self, raw, value, items):
        if re.fullmatch(r"(?:luu|nho|ghi nho) ten (?:toi|minh)", value):
            return self.request("memory.remember", {"memory_key": "display_name"})
        style = re.fullmatch(r"(?:tu gio |tu nay )?(?:hay )?xung (.+)", value)
        if style:
            return self.request("memory.remember", {"memory_key": "address_style", "memory_value": style[1]})
        if re.fullmatch(
            r"(?:hay )?(?:xoa|quen) (?:toan bo|all|tat ca) (?:bo nho|thong tin da nho)(?: cua toi)?|(?:xoa|quen) (?:tri|tri nho|ghi nho|bo nho|thong tin)(?: cua ban)? ve (?:toi|minh)",
            value,
        ):
            return self.request("memory.reset")

        def slug(name):
            return re.sub(r"[^a-z0-9]+", "-", normalize_text(name)).strip("-")[:60]

        named = re.fullmatch(
            r"(?:ghi nhớ|ghi nho|nhớ rằng|nho rang)\s+(lệnh|lenh|ghi chú|ghi chu)\s+(.+?):\s*(.+?)[.!?]*",
            raw.strip(),
            re.I,
        )
        if named:
            name, body = named[2], named[3]
            is_command = normalize_text(named[1]) == "lenh"
            if is_command:
                frame = self.interpret(body, SessionState(), [])
                if frame.act != "request" or len(frame.actions) != 1:
                    return self.ask("Mình chỉ lưu lệnh có tên cho một thao tác đủ thông tin.")
                from src.vivi.capabilities.registry import REGISTRY

                action = frame.actions[0]
                if action.capability not in REGISTRY or REGISTRY[action.capability].executor != "vehicle":
                    return self.ask("Lệnh này chưa được hỗ trợ.")
                if (
                    action.arguments.temperature_delta_celsius is None
                    and REGISTRY[action.capability].validate(action)[1]
                ):
                    return self.ask("Lệnh này còn thiếu thông tin hoặc giá trị chưa phù hợp.")
            return self.request(
                "memory.remember",
                {"memory_key": ("command." if is_command else "note.") + slug(name), "memory_value": body},
            )
        replay = re.fullmatch(r"(?:thuc hien|chay|dung) lenh (.+)", value)
        if replay:
            item = next((item for item in items if item["key"] == "command." + slug(replay[1])), None)
            if item:
                frame = self.interpret(str(item["value"]), SessionState(), [])
                if frame.act == "request" and len(frame.actions) == 1:
                    from src.vivi.capabilities.registry import REGISTRY

                    action = frame.actions[0]
                    if action.capability in REGISTRY and REGISTRY[action.capability].executor == "vehicle":
                        return frame
            return self.ask("Lệnh đã lưu chưa đủ thông tin hoặc chưa được hỗ trợ.")
        preference = re.fullmatch(
            r"(?:dat|chinh) (?:nhiet do|dieu hoa) (?:theo |ve )?(?:so thich(?: cua toi)?|muc toi thich)", value
        )
        music_preference = re.fullmatch(r"(?:phat|mo) nhac (?:toi thich|theo so thich(?: cua toi)?)", value)
        if preference or music_preference:
            key = "preferred_temperature" if preference else "preferred_music"
            item = next((item for item in items if item["key"] == key), None)
            return (
                self.request(
                    "climate.set_temperature" if preference else "media.play",
                    {"value_celsius" if preference else "media_query": item["value"]},
                )
                if item
                else self.ask("Mình chưa nhớ sở thích này. Bạn nói mức muốn đặt nhé.")
            )
        recall_target = re.fullmatch(r"(?:ban |vivi )?(?:co )?(?:con )?nho gi ve (.+?)(?: khong)?", value)
        if recall_target and recall_target[1] != "toi":
            key = "note." + slug(recall_target[1])
            return self.request("memory.recall", {"memory_key": key})
        if re.search(r"^(?:tu nay |tu gio |hay |ban hay )?(?:goi toi|ghi nho|nho rang|luu vao bo nho)", value):
            name = re.search(
                r"(?:gọi tôi(?: là)?|goi toi(?: la)?|tôi tên(?: là)?|toi ten(?: la)?)\s+(.+?)[.!?]*$", raw, re.I
            )
            if name:
                return self.request("memory.remember", {"memory_key": "display_name", "memory_value": name[1]})
            number = re.search(r"(?:nhiet do|dieu hoa).*?(\d+(?:[.,]\d+)?)", value)
            if number and not re.search(r"\b(?:neu|khi)\b", value):
                return self.request(
                    "memory.remember", {"memory_key": "preferred_temperature", "memory_value": number[1]}
                )
            music = re.search(r"(?:thích nhạc|thich nhac)\s+(.+?)[.!?]*$", raw, re.I)
            if music:
                return self.request("memory.remember", {"memory_key": "preferred_music", "memory_value": music[1]})
            note = re.search(
                r"(?:ghi nhớ|ghi nho|nhớ rằng|nho rang|lưu vào bộ nhớ|luu vao bo nho)\s*[:,-]?\s*(.+)", raw, re.I
            )
            if note:
                import hashlib

                key = "note." + hashlib.sha256(normalize_text(note[1]).encode()).hexdigest()[:12]
                return self.request("memory.remember", {"memory_key": key, "memory_value": note[1]})
        if re.fullmatch(
            r"(?:ban |vivi )?(?:co )?(?:con )?(?:nho gi ve toi|nho so thich cua toi|da nho nhung gi|da luu nhung gi|biet gi ve toi)(?: khong)?|(?:cho toi )?(?:xem|doc) (?:lai )?bo nho(?: cua toi)?",
            value,
        ):
            return self.request("memory.recall")
        if re.search(r"(?:toi ten|ten (?:cua )?toi).*(?:gi)|nho ten", value):
            return self.request("memory.recall", {"memory_key": "display_name"})
        if re.search(r"(?:biet|nho).*(?:toi|minh) thich (?:gi|nhung gi)", value):
            return self.request("memory.recall", {"memory_key": "preferences"})
        if re.fullmatch(r"nhiet do toi thich(?: la (?:bao nhieu|may do))?|toi thich nhac gi", value):
            return self.request(
                "memory.recall", {"memory_key": "preferred_temperature" if "nhiet do" in value else "preferred_music"}
            )
        if re.fullmatch(r"(?:toi ten(?: la)? (?:gi)|ten (?:cua )?toi (?:la gi)(?: nhi)?)", value):
            return self.request("memory.recall", {"memory_key": "display_name"})
        forgotten = re.fullmatch(r"(?:hay )?(?:quen|xoa ghi nho|xoa khoi bo nho) (.+)", value)
        if forgotten:
            aliases = {"ten": "display_name", "nhiet do": "preferred_temperature", "nhac": "preferred_music"}
            key = next(
                (
                    key
                    for word, key in aliases.items()
                    if re.fullmatch(word + r"(?: cua toi| toi thich| toi)?", forgotten[1])
                ),
                None,
            )
            if forgotten[1].startswith(("ghi chu ", "lenh ")):
                category, name = (
                    ("command", forgotten[1][5:]) if forgotten[1].startswith("lenh ") else ("note", forgotten[1][8:])
                )
                key = category + "." + slug(name)
            if not key:
                matches = [
                    item
                    for item in items
                    if forgotten[1] in normalize_text(str(item["value"]) + " " + item.get("label", ""))
                ]
                key = matches[0]["key"] if len(matches) == 1 else None
            return (
                self.request("memory.forget", {"memory_key": key})
                if key
                else self.ask("Bạn muốn mình quên mục nào đã lưu?")
            )
        return None

    @staticmethod
    def journey_arguments(raw):
        """Parse journey grammar into slots; vendor queries contain only names."""
        raw = strip_fillers(raw)
        if len(Lexer().clauses(raw)) != 1:
            return None
        normalized = normalize_text(raw).strip()
        prefix = re.match(
            r"^(?:(?:chi duong|dan duong) va (?:lap|len) lo trinh(?: di)?(?: va)? sac|(?:(?:lap|len) (?:ke hoach|lo trinh)|ke hoach|lo trinh|duong di)(?: di)?(?: va(?: lo trinh)? sac| sac)?(?: va di)?|(?:toi|minh) (?:muon|can) (?:den|toi|di(?: den| toi)?|ve)|(?:chi duong|dan duong|dan (?:toi|minh)|(?:dua|cho) (?:toi|minh)|di|ve))(?: den| toi)?\s+(.+)$",
            normalized,
        )
        if not prefix:
            return None
        planning = bool(re.search(r"ke hoach|lo trinh", normalized[: prefix.start(1)]))
        target = raw.strip()[prefix.start(1) :].strip()
        trip_kind = (
            "round_trip"
            if re.search(r"(?:va |roi )?(?:quay lai|ve lai)|khu hoi|ca di (?:lan|va) ve", normalize_text(target))
            else "one_way"
        )
        end = re.search(
            r"\s+(?:(?:va|roi) )?(?:quay lai|ve lai|khu hoi|ca di (?:lan|va) ve)\b.*$", normalize_text(target)
        )
        if end:
            target = target[: end.start()].strip()
        origin_query = None
        origin = re.match(r"^tu (.+?) (?:den|toi|ve) (.+)$", normalize_text(target))
        if origin:
            source = target[origin.start(1) : origin.end(1)].strip()
            if normalize_text(source) not in {"day", "vi tri hien tai", "cho nay"}:
                origin_query = source
            target = target[origin.start(2) :].strip()
        target = re.sub(r"^(?:(?:đi vào|di vao|đi|di|vào|vao|đến|den|tới|toi|về|ve)\s+)+", "", target, flags=re.I)
        target = OfflineParser.place_query(target)
        # "quán cà phê gần nhất": nearness is a slot, never part of the name sent to the map.
        near = re.search(r"\s+(?:gần đây|gan day|gần nhất|gan nhat|gần|gan)$", target, flags=re.I)
        if near:
            target = target[: near.start()].strip()
        if not target:
            return None
        args = {
            "query": target,
            "trip_kind": trip_kind,
            "destination_scope": "region" if region_for(target) else "place",
        }
        if near:
            args["nearest"] = True
        if origin_query:
            args["origin_query"] = origin_query
        return planning, args

    def trip(self, value, raw, session):
        journey = self.journey_arguments(raw)
        if journey and journey[0]:
            return self.request("trip.plan", journey[1])
        plan = session.trip_plan
        if not plan or plan.calculation_status == "cancelled":
            return None
        if session.navigation_focus != "trip" and value != "huy ke hoach":
            return None
        identity = {"plan_id": plan.id, "route_revision": plan.revision}
        if (
            plan.clarification
            and plan.clarification.get("phase") in {"change_destination", "confirm_destination"}
            and re.fullmatch(r"bat dau(?: luon)?(?: hanh trinh)?", value)
        ):
            return self.request("trip.start", identity)
        if re.fullmatch(r"(?:huy|thoi)(?: tim dia diem| ke hoach| yeu cau| lap lo trinh)?", value):
            return self.request("trip.cancel", identity)
        current_index = session.active.current_index() if session.active else None
        if (
            not plan.clarification
            and plan.offer
            and (plan.offer.get("status") == "pending" or self.start_consent(raw, explicit=True))
            and (current_index is None or re.fullmatch(r"bat dau(?: luon)?(?: hanh trinh)?", value))
        ):
            if re.fullmatch(
                r"(?:bat dau(?: luon)?|chay|dong y|xac nhan|ok|co|u|duoc)(?: di| hanh trinh| dan duong)?", value
            ):
                return self.request("trip.start", identity)
            if value in {"khong", "khong di", "chua", "chua di", "de sau"}:
                return self.request("trip.decline", identity)
        if plan.clarification or plan.candidates and plan.status == "selecting":
            # This read-only adapter needs the plan and task ownership, not
            # copies of every unrelated journey and geometry in the session.
            scratch = SessionState(
                navigation=plan.model_dump(mode="json", include=set(NavigationState.model_fields)),
                active=session.active.model_copy(deep=True) if session.active else None,
            )
            if scratch.active:
                for step in scratch.active.steps:
                    if step.draft.capability.startswith("trip."):
                        step.draft.capability = step.draft.capability.replace("trip.", "navigation.")
            parsed = self.navigation(value, raw, scratch)
            if parsed and parsed.actions:
                action = parsed.actions[0]
                if action.capability in {"navigation.refine", "navigation.select", "navigation.plan"}:
                    args = action.arguments.model_dump(exclude_none=True)
                    capability = action.capability.replace("navigation.", "trip.")
                    if capability != "trip.plan":
                        args.update(identity)
                    return self.request(capability, args)
            if parsed:
                return parsed
        return None

    def navigation(self, value, raw, session):
        nav = session.navigation
        clarification = nav.clarification
        if clarification and clarification.get("revision") == nav.revision:
            from src.vivi.dialogue.answers import AnswerResolver

            answer = AnswerResolver().resolve(raw, session)
            if answer:
                return answer
        if self.awaiting_place(session) and not clarification:
            correction = re.match(r"^(?:không[, ]+|đổi (?:sang|thành) |ý là |tìm lại )(.+)$", raw.strip(), re.I)
            if correction:
                target = correction[1].strip()
                journey = self.journey_arguments(target)
                return self.request("navigation.refine", {"query": journey[1]["query"] if journey else target})
        if re.fullmatch(r"huy (?:tim|chon) dia diem", value):
            return self.request("navigation.cancel", {"route_revision": nav.revision})
        active = session.active
        pending_step = active.steps[active.current_index()] if active and active.current_index() is not None else None
        if value in {"huy", "huy yeu cau"} and (nav.status == "preview" or nav.offer or nav.pending or nav.clarification) and (
            pending_step is None or pending_step.draft.capability.startswith("navigation.")
        ):
            return SemanticTurn(act="request", task_ref="active" if pending_step else None, actions=[
                ActionDraft(capability="navigation.cancel", arguments={"route_revision": nav.revision}),
            ])
        if re.fullmatch(r"(?:ket thuc hanh trinh|khong di nua)", value):
            return self.request("navigation.end")
        if re.fullmatch(r"(?:dung|huy|tat)(?: hanh trinh| dan duong| mo phong)", value):
            return self.request("navigation.stop")
        if (nav.status in {"preview", "paused"} or nav.pending and nav.pending.get("navigation")) and re.fullmatch(
            r"(?:bat dau|chay|di|xac nhan|dong y|ok|co|u|duoc)(?: di| dan duong| hanh trinh| mo phong)?", value
        ):
            # A yes answers the pending step, whatever it is: "Mở tất cả cửa" → "OK"
            # must open the doors, never start a route left over from earlier.
            if session.active and session.active.current_index() is not None:
                return SemanticTurn(act="confirm", task_ref="active")
            return self.request("navigation.start", {"route_revision": nav.revision})
        selection = self.place_ordinal(value)
        station_energy = (nav.pending.get("navigation", {}).get("energy") or {}) if nav.pending else (nav.energy or {})
        candidates = station_energy.get("station_candidates", []) if selection and re.search(r"\btram\b", value) else []
        candidates = candidates or nav.candidates
        if selection and candidates:
            index = {"dau tien": 0, "mot": 0, "nhat": 0, "hai": 1, "ba": 2, "1": 0, "2": 1, "3": 2}.get(
                selection[1], int(selection[1]) - 1 if selection[1].isdigit() else 99
            )
            if 0 <= index < len(candidates):
                return self.request("navigation.select", {"place_handle": candidates[index]["handle"]})
        if selection and (candidates or clarification):
            return self.ask("Số đó không có trong danh sách. Bạn chọn tên hoặc số đang hiển thị nhé.")
        if re.fullmatch(r"(?:(?:chi duong|dan duong|di|dan toi) )?den (?:do|day|cho do)", value):
            if len(nav.candidates) == 1:
                return self.request("navigation.select", {"place_handle": nav.candidates[0]["handle"]})
            return self.ask("Bạn muốn đến địa điểm thứ mấy trong danh sách?")
        if journey := self.journey_arguments(raw):
            if not re.search(r"tram sac|hieu thuoc|nha thuoc", normalize_text(journey[1]["query"])):
                if clarification and not region_for(journey[1]["query"]):
                    destination = DestinationSpec.from_arguments(journey[1])
                    destination.raw_text = raw
                    if (not destination.area and clarification.get("region")
                        and not administrative_layer(destination.name)
                        and not landmark_for(destination.name)):
                        destination.area = AreaConstraint(text=clarification["region"], source="context", constraint="preferred")
                    return self.request("navigation.refine", {"query": journey[1]["query"], "destination": destination})
                return self.request("navigation.plan", journey[1])
        match = re.fullmatch(
            r"(?:tim|tim giup toi|(?:di|ve)(?: den| toi)?|dan (?:toi|minh)(?: den| toi)?|dan duong(?: den| toi)?|chi duong(?: den| toi)?|(?:duong di|lap lo trinh|lo trinh)(?: di)?(?: va(?: lo trinh)? sac)?(?: den| toi)?|toi tinh di tu day den) (.+)",
            value,
        )
        if match or re.search(r"^(?:tram sac|hieu thuoc|nha thuoc)\b", value):
            target = match[1] if match else value
            target = re.sub(
                r"(?: giup toi| gan day| gan nhat| gan| lap lo trinh di va sac| cho toi| di va sac| lap lo trinh sac)$",
                "",
                target,
            )
            category = (
                "charging"
                if re.search(r"tram sac", target)
                else "pharmacy"
                if re.search(r"(?:hieu|nha) thuoc", target)
                else None
            )
            query = "trạm sạc VinFast" if category == "charging" else "nhà thuốc" if category == "pharmacy" else target
            region = region_for(target)
            if not category:
                # Normalized text classifies grammar; preserve Vietnamese place
                # names for the vendor query and later speech.
                query = " ".join(raw.split()[-len((match[1] if match else value).split()) :])
                query = re.sub(
                    r"(?: giúp tôi| gần đây| gần nhất| gần| cho tôi| lập lộ trình đi và sạc| đi và sạc| lập lộ trình sạc)$",
                    "",
                    query,
                    flags=re.I,
                ).strip()
            if clarification and not category and not region:
                return self.request("navigation.refine", {"query": query})
            return self.request(
                "navigation.search" if value.startswith("tim") or not match else "navigation.plan",
                {
                    "query": query,
                    "category": category,
                    "nearest": bool(re.search(r"\bgan(?: (?:day|nhat))?$", value)),
                    "destination_scope": "region" if region else "place",
                },
            )
        if clarification:
            correction = self.destination_correction(raw)
            cleaned = correction or re.sub(r"^(?:không[, ]+|khong[, ]+|ý là |y la |đến |den |đổi (?:sang|thành) |doi (?:sang|thanh) )", "", raw.strip(), flags=re.I)
            if journey := self.journey_arguments(cleaned):
                cleaned = journey[1]["query"]
            cleaned = self.place_query(cleaned)
            if correction and self.command(self.lexer.normalize(cleaned)):
                return None
            if region_for(cleaned):
                return self.request("navigation.refine", {"query": cleaned})
            if value not in {"khong", "huy", "thoi", "dung"} and not self.command(value):
                arguments = {"query": cleaned}
                if correction:
                    destination = DestinationSpec.from_arguments(arguments)
                    destination.raw_text = raw
                    arguments["destination"] = destination
                return self.request("navigation.refine", arguments)
        return None

    @staticmethod
    def destination_correction(text):
        match = re.match(r"^(?:đổi|doi|thay)(?: (?:địa điểm|điểm đến|dia diem|diem den))? (?:sang|thành|thanh|tới|toi|đến|den|về|ve)\s+(.+)$", text.strip(), re.I)
        return match[1].strip() if match else None

    @staticmethod
    def place_query(text):
        """Remove spoken courtesy tails before sending a destination to maps."""
        value = text.strip(" ,.!?")
        return re.sub(
            r"(?:\s+(?:giúp tôi|giup toi|cho tôi|cho toi|nhé|nhe|đi|di|luôn|luon|thôi|thoi))+$",
            "", value, flags=re.I,
        ).strip()

    @staticmethod
    def destination_area(text, owner):
        """Legacy reader delegates slot semantics to the shared answer boundary."""
        from src.vivi.dialogue.answers import AnswerResolver

        session = SessionState(navigation=owner.model_dump(mode="json", include=set(NavigationState.model_fields)))
        answer = AnswerResolver().resolve(text, session)
        return answer.actions[0].arguments.query if answer and answer.actions else None

    @staticmethod
    def destination_choice(value, owner):
        """Compatibility entry point; question polarity lives in AnswerResolver."""
        from src.vivi.dialogue.answers import AnswerResolver
        from src.vivi.dialogue.questions import current_question

        session = SessionState(navigation=owner.model_dump(mode="json", include=set(NavigationState.model_fields)))
        question = current_question(session)
        return AnswerResolver.choice_role(value, question) if question else None

    def explicit_navigation_reference(self, text, session):
        """Complete region/landmark and explicit directions have bounded semantics."""
        text = strip_fillers(text)
        journey = self.journey_arguments(text)
        if not journey or len(self.lexer.clauses(text)) != 1 or re.search(
            r"\b(?:neu|lat nua|ti nua|ngay mai|ngay kia|sau \d+ phut)\b", normalize_text(text),
        ):
            return None
        destination = DestinationSpec.from_arguments(journey[1])
        strong = re.match(r"^(?:chi duong|dan duong|(?:dua|cho) (?:toi|minh)|(?:toi|minh) (?:muon|can) (?:den|toi|di|ve))\b", normalize_text(text))
        if not destination.complete() or not (strong or destination.selection == "region"):
            return None
        parsed = self.interpret(text, session)
        if parsed.act == "request" and len(parsed.actions) == 1 and parsed.actions[0].capability in {
            "navigation.plan", "trip.plan", "navigation.refine", "trip.refine",
        } and not (parsed.unresolved or parsed.negated or parsed.scheduled or parsed.conditional):
            return parsed
        return None

    @staticmethod
    def place_ordinal(value):
        return re.fullmatch(
            r"(?:(?:chon|den|di den|dan den) )?"
            r"(?:(?:cai|cho|tram(?: sac)?|dia diem|ket qua|so) )?"
            r"(?:thu )?(dau tien|mot|nhat|hai|ba|bon|tu|nam|[0-9]+)", value,
        )

    def place_reference(self, text, session):
        """Resolve only literal answers against the currently owned POI list."""
        value = self.lexer.normalize(text)
        index = session.active.current_index() if session.active else None
        prefix = session.active.steps[index].draft.capability.split(".")[0] if index is not None else session.navigation_focus
        if prefix not in {"navigation", "trip"}:
            return None
        trip = prefix == "trip" and session.trip_plan is not None
        owner = session.trip_plan if trip else session.navigation
        if query := self.destination_area(text, owner):
            arguments = {"query": query}
            if trip:
                arguments.update(plan_id=owner.id, route_revision=owner.revision)
            return self.request(f"{prefix}.refine", arguments)
        kind = self.destination_choice(value, owner)
        choice = next((c for c in (owner.clarification or {}).get("choices", []) if c["kind"] == kind), None)
        if choice:
            arguments = {"choice_handle": choice["handle"]}
            if trip:
                arguments.update(plan_id=owner.id, route_revision=owner.revision)
            return self.request(f"{prefix}.refine", arguments)
        if kind:
            return self.ask("Lựa chọn địa điểm đã thay đổi. Bạn chọn lại điểm đến nhé.")
        question = owner.clarification or {}
        pending = (owner.pending or {}).get("navigation") or {}
        energy = pending.get("energy") or owner.energy or {}
        candidates = [*owner.candidates, *energy.get("station_candidates", [])]
        if not (owner.status == "selecting" or owner.pending and owner.pending.get("status") == "selecting"
                or question or energy.get("station_candidates")):
            return None
        literal = self.place_ordinal(value) or any(
            value in {normalize_text(p["name"]), normalize_text(p.get("spoken_label", ""))}
            for p in candidates
        )
        if not literal:
            return None
        parsed = self.trip(value, text, session) if trip else self.navigation(value, text, session)
        if parsed and (parsed.act == "clarify" or parsed.actions and parsed.actions[0].capability in {
            "navigation.select", "navigation.refine", "trip.select", "trip.refine",
        }):
            return parsed
        return None

    @staticmethod
    def awaiting_place(session):
        nav = session.navigation
        if not (nav.status == "selecting" or nav.pending and nav.pending.get("status") == "selecting"):
            return False
        index = session.active.current_index() if session.active else None
        return index is None or (
            session.active.steps[index].draft.capability
            in {"navigation.plan", "navigation.refine", "navigation.search"}
            and session.active.steps[index].status == "waiting_slot"
        )

    def start_consent(self, text, *, explicit=False):
        """Safety evidence for executing an existing offer, never intent routing."""
        value = self.lexer.normalize(text)
        value = re.sub(r"^(?:dung|dong y|ok)[, ]+", "", value)
        value = value.replace("bat dau di", "bat dau")
        start = re.fullmatch(r"(?:bat dau|khoi hanh|chay|di)(?: hanh trinh| dan duong)?(?: ngay| luon)?", value)
        if explicit:
            return bool(start)
        return bool(
            start or affirmed(value)
        )

    def cancellation_reference(self, text, session):
        """A literal task cancellation never needs a model interpretation."""
        if self.lexer.normalize(text) not in {"huy", "huy yeu cau"}:
            return None
        result = self.interpret(text, session)
        if result.act == "cancel" and result.task_ref == "active":
            return result
        if result.act == "request" and len(result.actions) == 1 and result.actions[0].capability in {
            "navigation.cancel", "trip.cancel",
        }:
            return result
        return None

    def confirmation_reference(self, text, session):
        """Resolve a whole consent reply only against the question currently owned."""
        index = session.active.current_index() if session.active else None
        waiting = index is not None and session.active.steps[index].status == "waiting_confirmation"
        question = session.pending_question() or {}
        if not waiting and question.get("kind") != "start_plan":
            return None
        value = self.lexer.normalize(text)
        if not (self.start_consent(text) or value in {"khong", "chua", "chua di", "de sau"}):
            return None
        result = self.interpret(text, session)
        return result if result.act in {"confirm", "deny", "cancel", "request", "clarify"} else None

    def guard(self, text, semantic, session, memory=None, history=None):
        """Language evidence checks stay at the language boundary for all tiers."""
        from src.vivi.dialogue.answers import AnswerResolver
        from src.vivi.dialogue.questions import current_question

        # A model's stale target cannot be rebound to a fresh question merely
        # because the utterance also looks like an acknowledgment.
        for draft in semantic.drafts():
            prefix = draft.capability.split(".")[0]
            if prefix not in {"navigation", "trip"}:
                continue
            revision = session.trip_plan.revision if prefix == "trip" and session.trip_plan else session.navigation_workspace.revision
            if draft.arguments.route_revision is not None and draft.arguments.route_revision != revision:
                return self.ask("Yêu cầu đã thay đổi. Bạn chọn lại địa điểm hoặc kế hoạch hiện tại nhé.")
            if prefix == "trip" and draft.arguments.plan_id and (
                session.trip_plan is None or draft.arguments.plan_id != session.trip_plan.id
            ):
                return self.ask("Kế hoạch này không còn thuộc câu hỏi hiện tại. Bạn chọn lại nhé.")
        answer = AnswerResolver().resolve(text, session)
        if answer:
            return answer
        if affirmed(self.lexer.normalize(text)) and current_question(session) is None:
            return self.ask("Mình chưa có thao tác đang chờ xác nhận.")
        value = self.lexer.normalize(text)
        result = semantic.model_copy(deep=True)
        bounded = self.interpret(text, session, memory, history)
        if session.active and session.active.current_index() is not None and re.fullmatch(r"(?:xin )?chao(?: vivi| ban)?|vivi|hello|hi", value):
            return bounded
        # Explicit internet/weather/story requests cannot be answered as stale
        # model text or mistaken for handbook/live vehicle observations.
        if (bounded.act in {"request", "question", "modify"} and bounded.actions
            and all(a.capability in {"conversation.respond", "web.search", "weather.get_forecast", "knowledge.lookup"} for a in bounded.actions)):
            if self.search_followup(value, session) and bounded.actions[0].capability == "web.search":
                return bounded
            if (bounded.act != "modify" and result.act == bounded.act
                and [a.capability for a in result.actions] == [a.capability for a in bounded.actions]):
                # The model can resolve richer locations and follow-ups. The
                # bounded grammar repairs routing; it must not erase valid slots.
                return result
            if (bounded.actions[0].capability == "conversation.respond"
                and bounded.actions[0].arguments.mode == "general" and result.act in {"question", "request"}
                and result.actions and all(a.capability in {"conversation.respond", "knowledge.lookup", "web.search"} for a in result.actions)):
                # A stable knowledge shortcut must not erase an explicit,
                # valid model proposal to verify it against external sources.
                return result
            return bounded
        if bounded.negated or bounded.conditional or bounded.scheduled or bounded.conflicting:
            return bounded
        journey = self.journey_arguments(text)
        if journey and region_for(journey[1]["query"]):
            # A newly named city/centre is destination evidence, not consent
            # to select an unrelated POI handle left over from an old search.
            return self.request("trip.plan" if journey[0] else "navigation.plan", journey[1])
        if (journey and administrative_layer(journey[1]["query"])) or any(
            a.capability in {"navigation.refine", "trip.refine"} and administrative_layer(a.arguments.query)
            for a in bounded.actions
        ):
            # Preserve the spoken ward/province qualifier when a model drops it.
            # The bounded parser also keeps a pending refinement's ownership.
            if bounded.actions and all(
                a.capability.startswith(("navigation.", "trip.")) for a in bounded.actions
            ):
                return bounded
        reference = self.place_reference(text, session)
        if reference:
            return reference
        steps = [a.capability for a in bounded.actions]
        if (
            bounded.act == "request"
            and not bounded.unresolved
            and "navigation.search" in steps
            and any(a.capability == "navigation.plan" and a.arguments.use_search_results for a in bounded.actions)
            and (result.act != "request" or [a.capability for a in result.drafts()] != steps)
        ):
            # "Tìm quán phở, đặt 24 độ rồi dẫn đường" is a complete new task whose order
            # the words fix. Logs 942-948: models dropped the search, put directions
            # first, or appended the steps to an old task.
            return bounded
        travel = [a for a in result.drafts() if a.capability.startswith(("navigation.", "trip."))]
        for action in travel:
            if action.capability == "trip.start":
                plan = session.trip_plan
                if plan and (action.arguments.plan_id != plan.id or action.arguments.route_revision != plan.revision):
                    return self.ask("Kế hoạch đã thay đổi. Bạn chọn lại nhé.")
                if plan and plan.status == "selecting" and not plan.clarification:
                    if len(plan.candidates) == 1 and self.start_consent(text):
                        return self.request(
                            "trip.select",
                            {
                                "place_handle": plan.candidates[0]["handle"],
                                "plan_id": plan.id,
                                "route_revision": plan.revision,
                            },
                        )
                    return self.ask("Bạn chọn địa điểm nào trong danh sách?")
                current = session.active.current_index() if session.active else None
                another_question = current is not None and not session.active.steps[
                    current
                ].draft.capability.startswith("trip.")
                explicit = (
                    bool(plan and plan.clarification)
                    or another_question
                    or bool(plan and plan.offer and plan.offer.get("status") != "pending")
                )
                if not self.start_consent(text, explicit=explicit):
                    return self.ask(
                        (
                            plan.clarification
                            if plan and plan.clarification
                            else plan.offer
                            if plan and plan.offer
                            else {}
                        ).get("question", "Bạn muốn bắt đầu hành trình này không?")
                    )
            owner = session.trip_plan if action.capability.startswith("trip.") else session.navigation
            if (
                action.capability.startswith("trip.")
                and action.capability != "trip.plan"
                and (
                    not owner
                    or action.arguments.plan_id != owner.id
                    or action.arguments.route_revision != owner.revision
                )
            ):
                return self.ask("Kế hoạch đã thay đổi. Bạn chọn lại nhé.")
            if action.arguments.place_handle and (
                not owner
                or not any(
                    p.get("handle") == action.arguments.place_handle
                    for p in [*owner.candidates, *(owner.energy or {}).get("station_candidates", [])]
                )
            ):
                return self.ask("Địa điểm không còn trong danh sách. Bạn chọn lại nhé.")
            if action.arguments.choice_handle and (
                not owner
                or not any(
                    c.get("handle") == action.arguments.choice_handle
                    for c in (owner.clarification or {}).get("choices", [])
                )
            ):
                return self.ask("Lựa chọn đã thay đổi. Bạn chọn lại nhé.")
        current = session.active.current_index() if session.active else None
        # Typed place answers are resolved by the dialogue reducer for every provider.
        current = session.active.current_index() if session.active else None
        if (
            result.act == "confirm"
            and current is None
            and session.trip_plan
            and session.trip_plan.offer
            and not self.start_consent(text, explicit=session.trip_plan.offer.get("status") != "pending")
        ):
            return self.ask(session.trip_plan.offer["question"])
        if (
            result.act == "confirm"
            and current is not None
            and session.active.steps[current].status == "waiting_confirmation"
            and session.active.steps[current].draft.capability.startswith("navigation.")
            and not self.start_consent(text)
        ):
            return self.ask("Bạn xác nhận bắt đầu hành trình này không?")
        if travel and len(travel) == len(result.drafts()):
            return result
        if (
            not travel
            and result.act in {"request", "reply"}
            and bounded.act == "request"
            and bounded.actions
            and not result.drafts()
            and self.command(value)
        ):
            return bounded
        unknown_control = any(
            re.match(
                r"^(?:mo|dong|dat|chinh|tang|giam|ha|nang|bat|tat|phat|dung|khoa|suoi|doc|kiem|gui|goi|xoa|quen|ghi|nho)\b",
                self.lexer.normalize(fragment),
            )
            for fragment in bounded.unresolved
        )
        if not travel and unknown_control and result.drafts() and len(result.drafts()) < len(self.lexer.clauses(text)):
            return bounded
        if not travel and bounded.act == "modify" and session.active and session.active.current_index() is not None:
            # A literal slot/correction can constrain a model proposal. A full
            # model plan with explicit step patches retains its own scheduling
            # meaning; the fallback parser must not replace it wholesale.
            if (
                result.act in {"request", "modify"}
                and not result.step_updates
                and (
                    self.command(value) is None
                    and len(self.lexer.clauses(text)) == 1
                    or bounded.append_actions
                    and not result.append_actions
                )
            ):
                return bounded
        if (
            result.act == "modify"
            and result.task_ref == "active"
            and session.active
            and session.active.current_index() is None
            and bounded.act == "request"
            and bounded.actions
            and not bounded.unresolved
        ):
            return bounded
        if (
            not travel
            and result.act == "request"
            and result.unresolved
            and bounded.act == "request"
            and not bounded.unresolved
        ):
            return bounded
        if re.fullmatch(r"(?:tu gio |tu nay )?(?:hay )?xung .+", value):
            parsed = self.memory(text, value, memory or [])
            if parsed:
                return parsed
        # A missing numeric slot is not permission for the model to invent one.
        climate_clauses = [self.command(clause.text) for clause in self.lexer.clauses(text)]
        proposals, evidence = result.drafts(), bounded.drafts()
        ambiguous_target = re.fullmatch(
            r"(?:tang|giam|ha) (?:dieu hoa|nhiet do) (?:len|xuong) (\d+(?:[.,]\d+)?) do(?: c)?", value
        )
        ambiguous_target = bool(ambiguous_target and 16 <= float(ambiguous_target[1].replace(",", ".")) <= 30)
        for index, action in enumerate(proposals):
            if len(evidence) == len(proposals):
                parsed = evidence[index]
                if (
                    parsed.capability == action.capability
                    and parsed.excluded_zones is not None
                    and not parsed.arguments.model_dump(exclude_none=True)
                ):
                    # A literal exclusion-only correction cannot turn into a
                    # new positive target, including in explicit model patches.
                    action.arguments = parsed.arguments.model_copy(deep=True)
                    action.excluded_zones = parsed.excluded_zones
                    action.exclusion_mode = parsed.exclusion_mode
            # Explicit numeric meaning is evidence, not a model-chosen slot.
            # Repair relative/absolute swaps before the reducer merges a task.
            if (
                action.capability == "climate.set_temperature"
                and len(evidence) == len(proposals)
                and not ambiguous_target
            ):
                parsed = evidence[index]
                if parsed.capability == action.capability and (
                    parsed.arguments.value_celsius is not None or parsed.arguments.temperature_delta_celsius is not None
                ):
                    action.arguments.value_celsius = parsed.arguments.value_celsius
                    action.arguments.temperature_delta_celsius = parsed.arguments.temperature_delta_celsius
            literal = climate_clauses[index] if len(climate_clauses) == len(proposals) else None
            if (
                action.capability == "climate.set_temperature"
                and literal
                and literal.capability == action.capability
                and literal.arguments.value_celsius is None
                and literal.arguments.temperature_delta_celsius is None
            ):
                action.arguments.value_celsius = None
                action.arguments.temperature_delta_celsius = None
            if action.referenced_target and not re.search(r"\breference\b", value):
                action.referenced_target = False
            if (
                action.capability in {"door.set_open", "window.set_position", "door.set_lock", "seat.set_heat_level"}
                and re.search(r"\b(?:trai|phai)\b", value)
                and not re.search(r"driver|front_passenger|rear_left|rear_right|truoc|sau", value)
            ):
                action.arguments.zone = None
                action.arguments.zones = None
                action.arguments.target_side = "left" if re.search(r"\btrai\b", value) else "right"
                action.arguments.target_row = None
            if action.capability == "memory.remember" and action.arguments.memory_key == "display_name":
                candidate = action.arguments.memory_value
                if candidate and (
                    normalize_text(candidate) in {"toi", "minh", "ban", "ten toi"}
                    or normalize_text(candidate) not in normalize_text(text)
                ):
                    action.arguments.memory_value = None
        # Bounded corrections have an unambiguous contextual grammar. Repair a
        # semantic contradiction without changing provider or granting consent.
        if (
            re.fullmatch(r"trai|phai|sau|truoc", value)
            and session.active
            and session.active.current_index() is not None
        ):
            parsed = self.interpret(text, session, memory, history)
            if parsed.act == "modify":
                return parsed
        if result.act == "reply":
            parsed = self.interpret(text, session, memory, history)
            if parsed.act == "offer":
                return parsed
            if re.fullmatch(r"(?:tu gio |tu nay )?(?:hay )?xung .+", value) and parsed.act == "request":
                return parsed
            if any(
                line.strip() and line.strip() in result.text
                for line in __import__(
                    "src.vivi.dialogue.planner", fromlist=["SEMANTIC_INSTRUCTION"]
                ).SEMANTIC_INSTRUCTION.splitlines()
            ) or re.search(r"\b(?:da|se|mo|tim).*\b(?:cua|suoi ghe|hieu thuoc)\b", normalize_text(result.text)):
                result.text = parsed.text if parsed.act == "reply" else "Mình nghe bạn. Bạn cần mình hỗ trợ gì?"
        if result.act == "clarify" and not result.conflicting and not result.conditional and not result.scheduled:
            parsed = self.interpret(text, session, memory, history)
            if parsed.act == "request" and parsed.actions and not parsed.unresolved:
                from src.vivi.capabilities.registry import REGISTRY

                if any(
                    any(
                        field.is_required()
                        and key not in draft.arguments.model_dump(exclude_none=True)
                        and not (key == "value_celsius" and draft.arguments.temperature_delta_celsius is not None)
                        for key, field in REGISTRY[draft.capability].spec.parameters.model_fields.items()
                    )
                    for draft in parsed.actions
                ):
                    return parsed
        return result

    @staticmethod
    def question_reference(text, history):
        from src.vivi.rag.knowledge import find_topic

        topic = find_topic(text)
        if topic or not history or history[-1].get("route") != "handbook":
            return text
        normalized = normalize_text(text)
        if re.search(r"\b(?:no|day|do|ay)\b|tinh nang.*khac", normalized):
            previous = find_topic(history[-1]["query"])
            if previous:
                return f"{text} {previous.label}"
        return text

    @staticmethod
    def status_fields(value):
        if re.search(r"\b(?:cach|huong dan|lam sao|tai sao|khuyen nghi)\b", value):
            return []
        if re.fullmatch(r"ban co dang gap van de gi khong", value):
            return ["diagnostics"]
        if re.search(r"con di duoc bao lau", value):
            return ["range_time"]
        if re.match(r"^(?:mo|dong|ha|nang|khoa|dat|chinh|tang|giam|bat|tat)\b", value):
            return []
        if re.fullmatch(r"(?:kiem tra )?(?:trang thai|tinh trang)(?: cua)?(?: xe)?", value):
            return ["summary"]
        observation = re.search(r"hien tai|dang|con (?:di|bao)|bay gio|kiem tra|da.*chua", value)
        if not observation:
            return []
        fields = []
        for pattern, field in (
            (r"dieu hoa|nhiet do", "temperature"),
            (r"pin", "battery"),
            (r"lop", "tires"),
            (r"con di|quang duong", "range"),
            (r"khoa|cua|hood|trunk", "body"),
        ):
            if re.search(pattern, value):
                fields.append(field)
        return fields


def poi_matches(name, category):
    """Fallback POI identity grammar when no verified vendor category is set."""
    value = normalize_text(name)
    if category == "charging":
        return charging_poi_info(name) is not None
    patterns = {
        "pharmacy": r"\b(?:nha thuoc|hieu thuoc|pharmacy)\b",
    }
    return bool(re.search(patterns[category], value))


def charging_poi_info(name):
    """Identity evidence in a map POI, not a claim about its charger power."""
    value = normalize_text(name)
    if re.search(r"\b(?:xe may|xe dap|doi pin|e-scooter|scooter)\b", value):
        return None
    if not re.search(r"\b(?:tram sac|charging|sac dien)\b", value):
        return None
    if not re.search(r"\b(?:vinfast|v-green|vgreen)\b", value):
        return None
    return {"network": "vgreen", "rest_area": rest_area_poi(name)}


def rest_area_poi(name):
    return bool(re.search(r"\btram dung nghi\b", normalize_text(name)))
