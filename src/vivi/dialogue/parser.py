"""Offline language boundary: lexer, bounded clause grammar and semantic frames.

All dialogue keyword matching lives here. It never writes memory or sends tools.
Unknown control clauses fail as a whole, rather than executing a known fragment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.vivi.dialogue.contracts import ActionDraft, SemanticTurn, SessionState
from src.vivi.text import normalize_text

ZONES = ("driver", "front_passenger", "rear_left", "rear_right")
LEXICON = (
    (r"kinh lai", "window driver"),
    (r"(?:phia )?(?:truoc|hang truoc)(?: ben)? phai|ben ghe phu|ben phu|ghe phu", "front_passenger"),
    (r"(?:phia )?(?:truoc|hang truoc)(?: ben)? trai|ben ghe lai|ben tai|ben lai|ghe lai|tai xe", "driver"),
    (r"(?:phia |hang )?sau(?: ben)? trai", "rear_left"),
    (r"(?:phia |hang )?sau(?: ben)? phai", "rear_right"),
    (r"tat ca|toan bo|ca bon|bon cua|4 cua", "all"),
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
        value = normalize_text(text).strip(" .!?;")
        for pattern, replacement in LEXICON:
            value = re.sub(r"\b(?:" + pattern + r")\b", replacement, value)
        value = re.sub(r"^(?:ban |vivi )?co the (?=(?:mo|dong|ha|khoa|phat|dat|chinh)\b)", "", value)
        value = re.sub(r"(?: giup (?:toi|minh))? khong$", "", value) if re.match(r"^(?:mo|dong|ha|khoa|phat|dat|chinh)\b", value) else value
        value = re.sub(r"^(?:vivi(?: oi)?[, ]+|hay |ban hay |giup toi |giup minh |lam on |co the )+", "", value)
        value = re.sub(r"(?:[, ]+(?:nhe|di|thoi|a|voi|giup toi|giup minh|xuong))+$", "", value)
        return re.sub(r"\s+", " ", value).strip()

    def clauses(self, text):
        # Splitting before parsing requires every conjunct to produce a frame.
        raw = re.split(r"\s+(?:và|va|rồi|roi|sau đó|sau do)\s+(?=(?:mở|mo|đóng|dong|đặt|dat|chỉnh|chinh|tăng|tang|giảm|giam|hạ|ha|bật|bat|tắt|tat|phát|phat|dừng|dung|khoá|khóa|khoa|sưởi|suoi|đọc|doc|kiểm|kiem|gửi|gui|gọi|goi|xóa|xoá|xoa|quên|quen|ghi|nhớ|nho)\b)|[,;]\s*(?=(?:mở|mo|đóng|dong|đặt|dat|phát|phat|bật|bat|tắt|tat)\b)", text.strip(), flags=re.I)
        return [Clause(self.normalize(part), part.strip()) for part in raw if part.strip()]


class OfflineParser:
    def __init__(self):
        self.lexer = Lexer()

    def interpret(self, text: str, session: SessionState, memory=None, history=None) -> SemanticTurn:
        # A comfort complaint followed by one aligned adjustment describes one
        # command, not two independent increments. Both clauses are consumed.
        comfort = re.fullmatch(r"(?:Tôi|Toi|Mình|Minh)\s+(?:hơi|hoi)\s+(lạnh|lanh|nóng|nong)\s*,\s*(.+)", text.strip(), re.I)
        if comfort:
            command = self.command(self.lexer.normalize(comfort[2]))
            if command and command.capability == "climate.set_temperature":
                delta = command.arguments.temperature_delta_celsius
                direction = 1 if normalize_text(comfort[1]) == "lanh" else -1
                if delta is not None and delta * direction > 0:
                    return SemanticTurn(act="request", actions=[command])
                return self.ask("Bạn nói rõ nhiệt độ muốn đặt nhé.")
        value = self.lexer.normalize(text)
        task = session.active
        index = task.current_index() if task else None
        current = task.steps[index] if index is not None else None
        live = bool(current)
        if re.fullmatch(r"(?:toi )?xac nhan|co|dong y|duoc(?: roi)?|ok|oke|okay|vang|uh|u|(?:u |uh )?dung roi(?: mo)?|u mo|u xoa|lam|thuc hien", value):
            return SemanticTurn(act="confirm", task_ref="active") if live else self.ask("Mình chưa có thao tác đang chờ xác nhận.")
        if re.fullmatch(r"khong|khong can|thoi|thoi khoi|bo qua|huy|huy yeu cau|dung lam|huy thao tac|khong mo nua", value):
            return SemanticTurn(act="cancel", task_ref="active") if live else SemanticTurn(act="reply", text="Được nhé, mình bỏ qua yêu cầu đó.")
        if re.search(r"^(?:de do da|tam dung yeu cau|tam giu|tam thoi dung)", value) and live:
            return SemanticTurn(act="suspend", task_ref="active", text="Mình tạm giữ yêu cầu đó.")
        if re.search(r"^(?:tiep tuc|quay lai|lam tiep)", value):
            return SemanticTurn(act="resume", task_ref="active" if live else "suspended:0" if session.suspended else None)
        # A complete noun/target grammar keeps "and" inside target sets and
        # exclusions. Other recognized control conjunctions become ordered
        # frames before any single-question/status/memory shortcut can fire.
        clauses = self.lexer.clauses(text)
        literal_memory = re.match(r"^(?:ghi nhớ|ghi nho|nhớ rằng|nho rang)\s+(?:lệnh|lenh|ghi chú|ghi chu)\s+.+?:", text.strip(), re.I)
        if not literal_memory and len(clauses) > 1 and self.command(value) is None:
            actions = []
            for clause in clauses:
                frame = self.interpret(clause.raw, session, memory, history)
                if frame.act not in {"request", "question"} or frame.negated or frame.conditional or frame.scheduled or frame.conflicting or frame.unresolved or not frame.actions:
                    return self.ask("Mình chưa hiểu đủ toàn bộ thao tác. Bạn nói rõ các bước muốn thực hiện nhé.")
                actions.extend(frame.actions)
            if len(actions) > 5:
                return self.ask("Bạn chia yêu cầu thành tối đa năm thao tác mỗi lần nhé.")
            return SemanticTurn(act="request", actions=actions)
        if memory_turn := self.memory(text, value, memory or []):
            return memory_turn
        if re.search(r"\b(?:neu|lat nua|ti nua|ngay mai|sau \d+ phut)\b", value):
            return SemanticTurn(act="clarify", conditional=True, text="Mình chưa thực hiện theo điều kiện hoặc hẹn giờ. Bạn nói thao tác cần làm ngay nhé.")
        if re.match(r"^(?:dung|khong|thoi dung)\s+(?:mo|dong|phat|khoa|dat|ghi nho|luu)\b", value):
            return SemanticTurn(act="reply", negated=True, text="Được nhé, mình giữ nguyên.")
        if re.fullmatch(r"(?:xin )?chao(?: vivi| ban)?|vivi|hello|hi", value):
            name = next((item["value"] for item in memory or [] if item.get("key") == "display_name"), None)
            return SemanticTurn(act="reply", text=f"Mình đây, {name}. Bạn cần gì thế?" if name else "Mình đây, bạn cần gì thế?")
        if re.fullmatch(r"toi bao co sao ban khong mo nhac luon", value):
            return self.request("media.play")
        if re.fullmatch(r"cam on(?: ban| vivi)?", value):
            return SemanticTurn(act="reply", text="Không có gì nhé.")
        if re.search(r"\b(?:buon|chan qua)\b", value) and "buon ngu" not in value:
            return SemanticTurn(act="offer", text="Mình ở đây với bạn. Bạn muốn nghe nhạc không?", actions=[ActionDraft(capability="media.play")])
        if re.search(r"\b(?:nong|lanh)\b", value) and re.search(r"toi da|cao nhat|max|toi thieu", value):
            return SemanticTurn(act="clarify", conflicting=True, text="Bạn muốn chỉnh nhiệt độ hay tốc độ quạt? Bạn nói rõ mức muốn đặt nhé.")
        if re.fullmatch(r"trai|phai|sau|truoc", value):
            return self.ask("Bạn nói rõ vị trí cửa trước hay sau, bên trái hay phải nhé.")
        if live:
            cleaned = re.sub(r"^(?:khong[, ]+|doi (?:thanh|sang) |chi )", "", value)
            if patch := self.slot_patch(cleaned, current.draft):
                return SemanticTurn(act="modify", task_ref="active", actions=[patch])
        if re.search(r"(?:tat|bypass|hack|vo hieu hoa).*(?:tui khi|phanh|pin cao ap)|(?:thao|mo|hack) pin cao ap", value):
            return SemanticTurn(act="unsupported", text="Mình không hướng dẫn can thiệp hệ thống an toàn hoặc điện cao áp.")
        if re.fullmatch(r"toi ten la .+", value):
            name = re.search(r"tôi tên là\s+([^,!.?]+)", text, re.I)
            return SemanticTurn(act="reply", text=f"Rất vui được làm quen với {name[1].strip() if name else 'bạn'}. Khi muốn lưu tên, bạn nói ghi nhớ nhé.")
        if re.fullmatch(r"(?:ban la (?:xe gi|ai)|ban co khoe khong|toi (?:hoi )?met|ban khong hieu y toi|tam biet)", value):
            return SemanticTurn(act="reply", text="Mình là ViVi, trợ lý trên xe. Mình ở đây nghe bạn; bạn có thể nói lại điều cần giúp nhé." if value != "tam biet" else "Tạm biệt, chúc bạn đi đường thuận lợi nhé.")
        if re.fullmatch(r"toi thich .+", value):
            return SemanticTurn(act="reply", text="Mình nghe bạn rồi. Khi muốn lưu thông tin, bạn nói ghi nhớ nhé.")
        if re.fullmatch(r"khong co gi[, ]+ban chay tiep|thoi bo qua", value):
            return SemanticTurn(act="reply", text="Được nhé, mình bỏ qua.")
        if re.fullmatch(r"(?:nhung ma )?(?:ban |vivi )?(?:giup gi duoc cho (?:toi|minh)|co the lam gi|giup duoc nhung gi(?: da)?)", value):
            return SemanticTurn(act="reply", text="Mình có thể chỉnh điều hoà, điều khiển cửa sổ, phát nhạc, đọc tình trạng xe, tra cẩm nang và ghi nhớ thông tin khi bạn yêu cầu.")
        if re.fullmatch(r"huong dan su dung xe|bo cam nang co nhung thong tin gi|toi muon hoi ve (?:chiec )?xe nay|ban biet gi ve (?:chiec )?xe nay", value):
            return self.ask("Bạn muốn tìm hiểu mục nào: pin, sạc, tính năng hay cách sử dụng xe?")
        if re.search(r"den.*(?:bao|canh bao).*(?:do|vang|mau)", value) and not re.search(r"phanh|pin|lop|tui khi", value):
            return self.ask("Bạn mô tả biểu tượng của đèn cảnh báo đó giúp mình nhé.")
        if re.fullmatch(r"(?:bat|tat) (?:cruise control|dieu hoa)|dap phanh", value):
            return SemanticTurn(act="unsupported", text="Mình chưa hỗ trợ điều khiển chức năng này bằng giọng nói.")
        if re.search(r"sac.*(?:bao lau|thoi gian)|(?:bao lau|thoi gian).*sac", value):
            mode = "DC" if re.search(r"\bdc\b|sac nhanh", value) else "AC" if re.search(r"\bac\b|sac cham", value) else None
            return self.request("manual.search", {"query": text, "question_kind": "charging_duration", "charging_mode": mode}, act="question")
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
        if re.match(r"^(?:mo|dong|dat|chinh|tang|giam|ha|bat|tat|phat|khoa|kich hoat)\b", value) and self.command(value) is None and not re.search(r"\b(?:cach|huong dan|lam sao|the nao|nhu nao|dung nut|nut nao|ra sao|xu ly)\b", value):
            return self.ask("Mình chưa hiểu đủ thao tác này. Bạn nói rõ yêu cầu nhé.")
        # Knowledge questions take precedence over imperatives inside a question.
        if re.search(r"\b(?:cach|huong dan|lam sao|tai sao|vi sao|la gi|o dau|bao lau|thong so|tinh nang|co nhung gi|co.*khong|the nao|nhu nao)\b", value):
            if re.search(r"(?:phanh|pin cao ap|tui khi).*(?:bypass|hack)|(?:bypass|hack|vo hieu hoa|thao pin|mo pin)", value):
                return SemanticTurn(act="unsupported", text="Mình không hướng dẫn can thiệp hệ thống an toàn hoặc điện cao áp.")
            # Explicit measurement cues distinguish observations from handbook facts.
            if re.search(r"hien tai|bay gio|con bao nhieu|con di duoc", value) and not re.search(r"cach|huong dan|tai sao", value):
                return self.request("vehicle.get_status")
            return self.request("manual.search", {"query": self.question_reference(text, history or [])}, act="question")
        if re.search(r"\b(?:trang thai xe|tinh trang xe|kiem tra pin|pin con|ap suat lop hien tai|xe con di|kiem tra xe)\b", value):
            return self.request("vehicle.get_status")
        if re.search(r"thoi tiet|gia ban|khuyen mai", value):
            return SemanticTurn(act="unsupported", text="Mình chưa có dịch vụ phù hợp để kiểm tra thông tin đó.")
        clauses = self.lexer.clauses(re.sub(r"\bcác\s+cửa\b", "cửa", text, flags=re.I))
        if len(clauses) > 5:
            return self.ask("Bạn chia yêu cầu thành tối đa năm thao tác mỗi lần nhé.")
        actions = []
        for clause in clauses:
            parsed = self.command(clause.text)
            if parsed is None:
                if len(clauses) > 1 or re.search(r"\b(?:mo|dong|dat|khoa|tang|giam|bat|tat|phat|ha)\b", clause.text):
                    return self.ask("Mình chưa hiểu đủ toàn bộ thao tác. Bạn nói rõ các bước muốn thực hiện nhé.")
                from src.vivi.rag.knowledge import find_topic
                if find_topic(text):
                    return self.request("manual.search", {"query": self.question_reference(text, history or [])}, act="question")
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

    @staticmethod
    def ask(text):
        return SemanticTurn(act="clarify", text=text)

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

    def slot_patch(self, value, draft):
        if draft.capability == "manual.search" and re.fullmatch(r"(?:sac )?(?:ac|dc|sac nhanh|sac cham)", value):
            return ActionDraft(capability="manual.search", arguments={"charging_mode": "DC" if value in {"dc", "sac dc", "sac nhanh"} else "AC"})
        if draft.capability in {"door.set_open", "door.set_lock", "window.set_position", "seat.set_heat_level"} and re.fullmatch(r"(?:o |phia |ben |cua |ghe )?(?:driver|front_passenger|rear_left|rear_right|all)", value):
            zone = value.split()[-1]
            return ActionDraft(capability=draft.capability, arguments={"zone": zone})
        if re.fullmatch(r"\d+(?:[.,]\d+)?(?: do(?: c)?)?", value) and draft.capability == "climate.set_temperature":
            return ActionDraft(capability=draft.capability, arguments={"value_celsius": float(value.split()[0].replace(",", "."))})
        if re.fullmatch(r"(?:mo )?50%", value) and draft.capability == "window.set_position":
            return ActionDraft(capability=draft.capability, arguments={"position_percent": 50})
        if re.fullmatch(r"(?:cua ra vao(?: xe)?|door|cua)", value) and draft.capability in {"body.set_open", "door.set_open"}:
            return ActionDraft(capability="door.set_open", arguments={"zone": "all", "open": draft.arguments.open})
        if value.startswith("tru "):
            zone = value.removeprefix("tru ").removeprefix("cua ").removeprefix("ben ")
            if zone in ZONES:
                return ActionDraft(capability=draft.capability, excluded_zones=[zone])
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
            exclusions = [re.sub(r"^(?:cua |window |door |ben )+", "", item.strip()) for item in re.split(r"\s+va\s+|,\s*", excluded)]
            if not exclusions or any(zone not in ZONES for zone in exclusions) or len(set(exclusions)) != len(exclusions):
                return None
        # A bounded noun/target/value grammar: every token is consumed.
        match = re.fullmatch(r"(mo|dong|ha|nang|len|khoa|mo khoa|bat|tat)\s+(?:(all)\s+)?(window|kinh|door|cua|hood|trunk)(?:\s+(.*))?", value)
        if match:
            verb, all_target, noun, suffix = match.groups()
            suffix = suffix or ""
            percent = re.search(r"(?:^|\s)(\d+)\s*%(?:$|\s)", suffix)
            if percent:
                suffix = (suffix[:percent.start()] + " " + suffix[percent.end():]).strip()
                suffix = re.sub(r"(?:\s+|^)(?:xuong|len|toi|o muc)$", "", suffix).strip()
            suffix = re.sub(r"^(?:o |ben |phia )+", "", suffix)
            suffix = re.sub(r"\s+(?:o |ben |phia )", " ", suffix)
            targets = [re.sub(r"^(?:ben |phia )+", "", token.strip()) for token in re.split(r"\s+va\s+|,\s*", suffix)]
            zones = targets if len(targets) > 1 and all(target in ZONES for target in targets) and len(set(targets)) == len(targets) else None
            zone = "all" if all_target else suffix if suffix in (*ZONES, "all") else None
            reference = suffix == "reference"
            if suffix in {"phia sau", "sau", "dang sau"} and noun in {"door", "cua"}:
                suffix = ""
            if suffix and zone is None and zones is None and not reference:
                return None
            args = {"zone": zone} if zone else {"zones": zones} if zones else {}
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
                capability, args = ("hood.set_open" if noun == "hood" else "trunk.set_open"), {"open": verb in {"mo", "bat", "nang"}}
            return ActionDraft(capability=capability, arguments=args, excluded_zones=exclusions, referenced_target=reference)
        limit = re.fullmatch(r"(?:tang|giam|ha|dat|chinh) (?:dieu hoa|nhiet do)(?: len| xuong| ve)? (toi da|cao nhat|toi thieu|thap nhat)", value)
        if limit:
            return ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": 30 if limit[1] in {"toi da", "cao nhat"} else 16})
        relative = re.fullmatch(r"(?:tang|giam|ha) (?:dieu hoa|nhiet do)(?: them| bot)? (\d+(?:[.,]\d+)?) do", value)
        if relative:
            return ActionDraft(capability="climate.set_temperature", arguments={"temperature_delta_celsius": float(relative[1].replace(",", ".")) * (-1 if value.startswith(("giam", "ha")) else 1)})
        match = re.fullmatch(r"(?:dat|chinh|tang(?: len)?|giam(?: xuong)?|ha)\s+(?:nhiet do|dieu hoa)(?:\s+(?:o|ve|den|len|xuong|thanh))?(?:\s+(\d+(?:[.,]\d+)?)(?: do(?: c)?)?)?", value)
        if match:
            return ActionDraft(capability="climate.set_temperature", arguments={"value_celsius": float(match[1].replace(",", "."))} if match[1] else {})
        match = re.fullmatch(r"(?:tang|giam|ha)(?: them| bot)?\s+(\d+(?:[.,]\d+)?) do", value)
        if match:
            return ActionDraft(capability="climate.set_temperature", arguments={"temperature_delta_celsius": float(match[1].replace(",", ".")) * (-1 if value.startswith(("giam", "ha")) else 1)})
        if re.fullmatch(r"(?:toi |minh |hoi |qua )*(?:lanh|nong)(?: qua)?|(?:mat|am) hon", value):
            return ActionDraft(capability="climate.set_temperature", arguments={"temperature_delta_celsius": -2 if "nong" in value or "mat" in value else 2})
        match = re.fullmatch(r"(?:bat |tat |chinh )?(?:suoi ghe|ghe suoi|lam am ghe)(?:\s+(all|driver|front_passenger|rear_left|rear_right))?(?:\s+(?:muc|cap)\s+([0-3]))?", value)
        if match:
            return ActionDraft(capability="seat.set_heat_level", arguments={**({"zone": match[1]} if match[1] else {}), "level": 0 if value.startswith("tat") else int(match[2] or 1)})
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
        if re.fullmatch(r"(?:hay )?(?:xoa|quen) (?:toan bo|all|tat ca) (?:bo nho|thong tin da nho)(?: cua toi)?|(?:xoa|quen) (?:tri|tri nho|ghi nho|bo nho|thong tin)(?: cua ban)? ve (?:toi|minh)", value):
            return self.request("memory.reset")
        def slug(name):
            return re.sub(r"[^a-z0-9]+", "-", normalize_text(name)).strip("-")[:60]
        named = re.fullmatch(r"(?:ghi nhớ|ghi nho|nhớ rằng|nho rang)\s+(lệnh|lenh|ghi chú|ghi chu)\s+(.+?):\s*(.+?)[.!?]*", raw.strip(), re.I)
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
                if action.arguments.temperature_delta_celsius is None and REGISTRY[action.capability].validate(action)[1]:
                    return self.ask("Lệnh này còn thiếu thông tin hoặc giá trị chưa phù hợp.")
            return self.request("memory.remember", {"memory_key": ("command." if is_command else "note.") + slug(name), "memory_value": body})
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
        preference = re.fullmatch(r"(?:dat|chinh) (?:nhiet do|dieu hoa) (?:theo |ve )?(?:so thich(?: cua toi)?|muc toi thich)", value)
        music_preference = re.fullmatch(r"(?:phat|mo) nhac (?:toi thich|theo so thich(?: cua toi)?)", value)
        if preference or music_preference:
            key = "preferred_temperature" if preference else "preferred_music"
            item = next((item for item in items if item["key"] == key), None)
            return self.request("climate.set_temperature" if preference else "media.play", {"value_celsius" if preference else "media_query": item["value"]}) if item else self.ask("Mình chưa nhớ sở thích này. Bạn nói mức muốn đặt nhé.")
        recall_target = re.fullmatch(r"(?:ban |vivi )?(?:co )?(?:con )?nho gi ve (.+?)(?: khong)?", value)
        if recall_target and recall_target[1] != "toi":
            key = "note." + slug(recall_target[1])
            return self.request("memory.recall", {"memory_key": key})
        if re.search(r"^(?:tu nay |tu gio |hay |ban hay )?(?:goi toi|ghi nho|nho rang|luu vao bo nho)", value):
            name = re.search(r"(?:gọi tôi(?: là)?|goi toi(?: la)?|tôi tên(?: là)?|toi ten(?: la)?)\s+(.+?)[.!?]*$", raw, re.I)
            if name:
                return self.request("memory.remember", {"memory_key": "display_name", "memory_value": name[1]})
            number = re.search(r"(?:nhiet do|dieu hoa).*?(\d+(?:[.,]\d+)?)", value)
            if number and not re.search(r"\b(?:neu|khi)\b", value):
                return self.request("memory.remember", {"memory_key": "preferred_temperature", "memory_value": number[1]})
            music = re.search(r"(?:thích nhạc|thich nhac)\s+(.+?)[.!?]*$", raw, re.I)
            if music:
                return self.request("memory.remember", {"memory_key": "preferred_music", "memory_value": music[1]})
            note = re.search(r"(?:ghi nhớ|ghi nho|nhớ rằng|nho rang|lưu vào bộ nhớ|luu vao bo nho)\s*[:,-]?\s*(.+)", raw, re.I)
            if note:
                import hashlib
                key = "note." + hashlib.sha256(normalize_text(note[1]).encode()).hexdigest()[:12]
                return self.request("memory.remember", {"memory_key": key, "memory_value": note[1]})
        if re.fullmatch(r"(?:ban |vivi )?(?:co )?(?:con )?(?:nho gi ve toi|nho so thich cua toi|da nho nhung gi|da luu nhung gi|biet gi ve toi)(?: khong)?|(?:cho toi )?(?:xem|doc) (?:lai )?bo nho(?: cua toi)?", value):
            return self.request("memory.recall")
        if re.search(r"(?:toi ten|ten (?:cua )?toi).*(?:gi)|nho ten", value):
            return self.request("memory.recall", {"memory_key": "display_name"})
        if re.search(r"(?:biet|nho).*(?:toi|minh) thich (?:gi|nhung gi)", value):
            return self.request("memory.recall", {"memory_key": "preferences"})
        if re.fullmatch(r"nhiet do toi thich(?: la (?:bao nhieu|may do))?|toi thich nhac gi", value):
            return self.request("memory.recall", {"memory_key": "preferred_temperature" if "nhiet do" in value else "preferred_music"})
        if re.fullmatch(r"(?:toi ten(?: la)? (?:gi)|ten (?:cua )?toi (?:la gi)(?: nhi)?)", value):
            return self.request("memory.recall", {"memory_key": "display_name"})
        forgotten = re.fullmatch(r"(?:hay )?(?:quen|xoa ghi nho|xoa khoi bo nho) (.+)", value)
        if forgotten:
            aliases = {"ten": "display_name", "nhiet do": "preferred_temperature", "nhac": "preferred_music"}
            key = next((key for word, key in aliases.items() if re.fullmatch(word + r"(?: cua toi| toi thich| toi)?", forgotten[1])), None)
            if forgotten[1].startswith(("ghi chu ", "lenh ")):
                category, name = ("command", forgotten[1][5:]) if forgotten[1].startswith("lenh ") else ("note", forgotten[1][8:])
                key = category + "." + slug(name)
            if not key:
                matches = [item for item in items if forgotten[1] in normalize_text(str(item["value"]) + " " + item.get("label", ""))]
                key = matches[0]["key"] if len(matches) == 1 else None
            return self.request("memory.forget", {"memory_key": key}) if key else self.ask("Bạn muốn mình quên mục nào đã lưu?")
        return None


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
        for pattern, field in ((r"dieu hoa|nhiet do", "temperature"), (r"pin", "battery"), (r"lop", "tires"), (r"con di|quang duong", "range"), (r"khoa|cua|hood|trunk", "body")):
            if re.search(pattern, value):
                fields.append(field)
        return fields
