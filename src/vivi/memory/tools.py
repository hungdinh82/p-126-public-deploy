"""Memory tool execution is authoritative; the language model cannot claim a write."""

from src.vivi.agents.contracts import IntentDecision
from src.vivi.agents.dialogue import clarify, respond
from src.vivi.agents.tools import validate_tool_arguments
from src.vivi.memory.sqlite import MemoryValue


def reset_offer() -> IntentDecision:
    decision = clarify("Bạn muốn xoá toàn bộ thông tin mình đã ghi nhớ về bạn phải không?")
    from src.vivi.agents.contracts import FollowUp
    decision.follow_up = FollowUp(intent="memory.reset")
    return decision


def execute_memory_tool(store, profile_id, decision, *, session_id, turn_id) -> IntentDecision:
    arguments, error = validate_tool_arguments(decision.intent, decision.arguments.model_dump(exclude_none=True))
    if error:
        return clarify(error)
    if store is None:
        return respond("Mình chưa truy cập được bộ nhớ đã lưu.")
    if decision.intent == "memory.reset":
        return reset_offer()
    key = arguments.get("memory_key")
    if decision.intent == "memory.recall":
        items = store.list(profile_id)
        if key == "preferences":
            items = [item for item in items if item["kind"] == "preference"]
        elif key:
            items = [item for item in items if item["key"] == key]
        if not items:
            return respond("Mình chưa được bạn dặn ghi nhớ sở thích nào." if key == "preferences"
                           else "Mình chưa ghi nhớ thông tin đó về bạn.")
        text = "; ".join(f"{item['label']}: {str(item['value'])[:100]}" for item in items[:3])
        return respond(f"Mình đang nhớ: {text}.")
    if decision.intent == "memory.forget":
        removed = store.forget(profile_id, key)
        return respond("Mình đã quên thông tin đó." if removed else "Mình chưa lưu thông tin đó.")
    if decision.intent == "memory.remember":
        if key.startswith("command."):
            return clarify("Để lưu lệnh, bạn nói ‘ghi nhớ lệnh’, tên lệnh và thao tác nhé.")
        item = MemoryValue(key=key, kind="note", value=arguments["memory_value"])
        store.remember(profile_id, item, session_id=session_id, turn_id=turn_id)
        return respond(f"Mình đã nhớ: {item.label.lower()} là {item.value}.")
    raise ValueError("Unknown memory tool")
