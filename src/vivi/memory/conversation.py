from __future__ import annotations

import hashlib
import re
import unicodedata

from src.vivi.agents.action_validation import validate_action_arguments
from src.vivi.agents.classifier import RulesIntentClassifier, priority_decision
from src.vivi.agents.contracts import IntentDecision
from src.vivi.memory.sqlite import MemoryValue, SQLiteLongTermMemory
from src.vivi.text import normalize_text


def _reply(text: str, *, clarify=False) -> IntentDecision:
    return IntentDecision(
        route="clarify" if clarify else "conversation",
        intent="conversation.clarify" if clarify else "conversation.respond",
        response_text=text,
        needs_clarification=clarify,
        clarification_question=text if clarify else None,
    )


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text(name)).strip("-")
    if len(slug) > 60:
        slug = slug[:40] + "-" + hashlib.sha256(slug.encode()).hexdigest()[:16]
    return slug


def _text(text: str) -> tuple[str, str]:
    raw = unicodedata.normalize("NFC", text).strip().rstrip(".?!")
    raw = re.sub(r"^(?:ViVi(?:\s+ơi)?[,!]?\s+)", "", raw, flags=re.I)
    return raw, normalize_text(raw)


def _value_text(item: dict) -> str:
    value = item["value"]
    if item["key"] == "preferred_temperature":
        return f"{float(value):g} độ C"
    return str(value)


def handle_memory_request(
    store: SQLiteLongTermMemory | None, profile_id: str, text: str, *, session_id: str, turn_id: str,
    vehicle_state: dict | None = None,
) -> IntentDecision | None:
    """Only explicit memory operations write; dialogue/logs never get mined automatically."""
    raw, normalized = _text(text)
    if re.match(r"^(?:dung|khong|thoi dung) (?:ghi nho|nho rang|luu vao bo nho)\b", normalized):
        return _reply("Được nhé, mình không lưu thông tin này.")
    remember = re.match(r"^(?:(?:hay |ban hay |giup toi )?(?:ghi nho|nho rang|luu vao bo nho)\s*[:,-]?\s*|tu nay )", normalized)
    recall = bool(re.fullmatch(r"(?:ban |vivi )?(?:co )?(?:con )?(?:nho gi ve toi|nho so thich cua toi|"
                              r"da nho nhung gi|da luu nhung gi|biet gi ve toi)(?: khong)?|"
                              r"(?:cho toi )?(?:xem|doc) (?:lai )?bo nho(?: cua toi)?", normalized))
    recall_target = re.fullmatch(r"(?:ban |vivi )?(?:co )?(?:con )?nho gi ve\s+(.+?)(?: khong)?", normalized)
    personal_question = re.fullmatch(r"(?:ban (?:co )?nho )?(?:toi (?:ten la gi|ten gi)|ten (?:cua )?toi (?:la gi|la gi nhi)|"
                                     r"nhiet do toi thich(?: la (?:bao nhieu|may do))?|toi thich nhac gi)|"
                                     r"(?:ban )?(?:co )?(?:con )?nho ten(?: cua)? toi(?: khong)?", normalized)
    forget = re.match(r"^(?:hay )?(?:quen|xoa khoi bo nho|xoa ghi nho)\s+", normalized)
    reset = bool(re.fullmatch(r"(?:hay )?(?:xoa|quen) (?:toan bo|tat ca) (?:bo nho|thong tin da nho)(?: cua toi)?", normalized))
    if not (remember or recall or recall_target or personal_question or forget or reset):
        return None
    if store is None:
        return _reply("Bộ nhớ dài hạn đang tắt hoặc chưa đọc được, nên mình chưa thể lưu hay xem thông tin.")
    if reset:
        store.reset(profile_id)
        return _reply("Mình đã xoá toàn bộ thông tin đã ghi nhớ của bạn.")
    items = store.list(profile_id)
    if recall:
        if not items:
            return _reply("Mình chưa ghi nhớ thông tin nào về bạn.")
        selected = items[:5]
        summary = "; ".join(f"{item['label']}: {_value_text(item)[:100]}" for item in selected)
        suffix = f" Còn {len(items) - len(selected)} mục khác." if len(items) > len(selected) else ""
        return _reply(f"Mình đang nhớ: {summary}.{suffix}")
    if recall_target:
        target = recall_target[1]
        selected = [item for item in items if target in normalize_text(f"{item['label']} {item['value']}")]
        if not selected:
            return _reply("Mình chưa ghi nhớ thông tin nào về điều đó.")
        return _reply("Mình đang nhớ: " + "; ".join(_value_text(item)[:150] for item in selected[:3]) + ".")
    if personal_question:
        key = ("preferred_temperature" if "nhiet do" in normalized else
               "preferred_music" if "nhac" in normalized else "display_name")
        item = next((item for item in items if item["key"] == key), None)
        return _reply(f"{item['label']} là {_value_text(item)}." if item else "Mình chưa được bạn dặn ghi nhớ thông tin này.")
    if forget:
        target = normalized[forget.end():].strip()
        aliases = {"ten": "display_name", "nhiet do": "preferred_temperature", "nhac": "preferred_music",
                   "cach tra loi": "response_style"}
        key = next((key for alias, key in aliases.items() if re.fullmatch(
            rf"(?:so thich |thong tin ve )?{alias}(?: (?:cua toi|toi thich|toi))?", target)), None)
        if target.startswith("lenh "):
            key = "command." + _slug(target[5:])
        named_note = re.match(r"(?:ghi chu|thong tin|yeu cau)\s+(.+)", target)
        if named_note:
            key = "note." + _slug(named_note[1])
        if key is None:
            matches = [item for item in items if target in normalize_text(f"{item['label']} {item['value']}")]
            if len(matches) != 1:
                return _reply("Bạn muốn mình quên thông tin nào? Hãy nói rõ tên, nhiệt độ, nhạc hoặc nội dung ghi chú.", clarify=True)
            key = matches[0]["key"]
        deleted = store.forget(profile_id, key)
        return _reply("Mình đã quên thông tin đó." if deleted else "Mình chưa lưu thông tin đó.")
    assert remember is not None
    body = raw[remember.end():].strip()
    normalized_body = normalize_text(body)
    if not body:
        return _reply("Bạn muốn mình ghi nhớ điều gì?", clarify=True)
    command = re.fullmatch(r"lenh\s+(.+?)\s*:\s*(.+)", normalized_body)
    named_note = re.fullmatch(r"(?:ghi chu|thong tin|yeu cau)\s+(.+?)\s*:\s*(.+)", normalized_body)
    if command:
        name = body[command.start(1):command.end(1)].strip(" '\"“”")
        transcript = body[command.start(2):command.end(2)].strip()
        if not _slug(name):
            return _reply("Bạn đặt tên ngắn cho lệnh này nhé.", clarify=True)
        decision = RulesIntentClassifier().classify_with_context(transcript, [], vehicle_state)
        if decision.route != "action" or decision.intent == "vehicle.get_status":
            return _reply("Mình chỉ lưu lệnh có tên cho một thao tác đã hỗ trợ và đủ thông tin. Bạn nói rõ thao tác nhé.", clarify=True)
        _, validation_error = validate_action_arguments(decision)
        if validation_error:
            return _reply(validation_error, clarify=True)
        item = MemoryValue(key="command." + _slug(name), kind="command", value=transcript, label=f"Lệnh {name}")
    elif named_note:
        name = body[named_note.start(1):named_note.end(1)].strip(" '\"“”")
        if not _slug(name):
            return _reply("Bạn đặt tên ngắn cho ghi chú này nhé.", clarify=True)
        item = MemoryValue(key="note." + _slug(name), kind="note",
                           value=body[named_note.start(2):named_note.end(2)], label=name)
    elif re.match(r"(?:goi toi (?:la )?|toi (?:ten la|ten) )", normalized_body):
        prefix = re.match(r"(?:goi toi (?:la )?|toi (?:ten la|ten) )", normalized_body)
        item = MemoryValue(key="display_name", kind="fact", value=body[prefix.end():])
    elif "nhiet do" in normalized_body or "dieu hoa" in normalized_body:
        number = re.search(r"(?<![\d.])-?\d+(?:[.,]\d+)?", normalized_body)
        # Do not reinterpret arbitrary conditional requirements as temperature preferences.
        preference = re.search(r"(?:toi thich|ua thich|yeu thich|mac dinh|thuong dung|cho toi)", normalized_body)
        if re.search(r"\b(neu|khi|truoc khi|sau khi)\b", normalized_body):
            preference = None
        if preference and number:
            item = MemoryValue(key="preferred_temperature", kind="preference", value=number[0].replace(",", "."))
        else:
            item = _note(body)
    elif re.match(r"toi thich (?:nghe )?nhac\s+", normalized_body):
        prefix = re.match(r"toi thich (?:nghe )?nhac\s+", normalized_body)
        item = MemoryValue(key="preferred_music", kind="preference", value=body[prefix.end():])
    elif re.search(r"tra loi (?:that )?(?:ngan gon|chi tiet)", normalized_body):
        item = MemoryValue(key="response_style", kind="preference",
                           value="ngắn gọn" if "ngan gon" in normalized_body else "chi tiết")
    else:
        item = _note(body)
    store.remember(profile_id, item, session_id=session_id, turn_id=turn_id)
    if item.kind == "command":
        return _reply(f"Mình đã nhớ {item.label.lower()}. Khi cần, bạn nói “thực hiện {item.label.lower()}” nhé.")
    return _reply(f"Mình đã nhớ: {item.label.lower()} là {_value_text(item.model_dump())}.")


def _note(body: str) -> MemoryValue:
    key = hashlib.sha256(normalize_text(body).encode()).hexdigest()[:20]
    return MemoryValue(key=f"note.{key}", kind="note", value=body, label="Ghi chú của bạn")


def memory_action_decision(store, profile_id, text, history, vehicle_state) -> IntentDecision | None:
    """Explicit replay only; output follows the ordinary validation/safety/verification graph."""
    _, normalized = _text(text)
    replay = re.fullmatch(r"(?:hay )?(?:thuc hien|chay|dung) lenh\s+(.+)", normalized)
    temperature = bool(re.fullmatch(r"(?:hay )?(?:dat|chinh) (?:nhiet do|dieu hoa) (?:theo |ve )?(?:so thich(?: cua toi)?|muc toi thich)", normalized))
    music = bool(re.fullmatch(r"(?:hay )?(?:phat|mo) nhac (?:toi thich|theo so thich(?: cua toi)?)", normalized))
    if not (replay or temperature or music):
        return None
    if store is None:
        return _reply("Mình chưa đọc được sở thích hoặc lệnh đã lưu. Bạn nói trực tiếp thao tác muốn làm nhé.", clarify=True)
    items = {item["key"]: item for item in store.list(profile_id)}
    key = "command." + _slug(replay[1]) if replay else "preferred_temperature" if temperature else "preferred_music"
    item = items.get(key)
    if item is None:
        return _reply("Mình chưa nhớ thông tin này. Bạn cho mình biết trước nhé.", clarify=True)
    if temperature:
        return IntentDecision(route="action", intent="climate.set_temperature", arguments={"value_celsius": item["value"]})
    if music:
        return IntentDecision(route="action", intent="media.play", arguments={"media_query": item["value"]})
    transcript = str(item["value"])
    decision = priority_decision(transcript) or RulesIntentClassifier().classify_with_context(transcript, [], vehicle_state)
    if decision.route != "action":
        return _reply("Lệnh đã lưu chưa đủ thông tin hoặc chưa được hỗ trợ. Bạn nói trực tiếp thao tác muốn làm nhé.", clarify=True)
    return decision
