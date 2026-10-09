"""Capability catalog and parameter boundary; contains no language interpretation."""
from __future__ import annotations

from dataclasses import dataclass

from src.vivi.agents.tools import TOOL_REGISTRY, validate_tool_arguments
from src.vivi.dialogue.contracts import ActionDraft
from src.vivi.vehicle.zones import CABIN_ZONES


@dataclass(frozen=True)
class Capability:
    name: str
    executor: str
    transferable: frozenset[str] = frozenset()

    @property
    def spec(self):
        return TOOL_REGISTRY[self.name]

    def validate(self, draft: ActionDraft, observation=None):
        args = draft.arguments.model_dump(exclude_none=True)
        if self.name == "manual.search" and args.get("question_kind") == "charging_duration" and not args.get("charging_mode"):
            return None, "Bạn muốn hỏi thời gian sạc AC hay sạc nhanh DC?"
        delta = args.pop("temperature_delta_celsius", None)
        if delta is not None:
            if self.name != "climate.set_temperature" or "value_celsius" in args:
                return None, "Bạn nói rõ nhiệt độ muốn đặt nhé."
            if observation is None:
                return None, "Mình chưa đọc được nhiệt độ hiện tại."
            args["value_celsius"] = min(30, max(16, observation.temperature_celsius + delta))
        if draft.excluded_zones:
            if not self.transferable.intersection({"zone", "zones"}):
                return None, "Thao tác này không hỗ trợ loại trừ vị trí."
            targets = list(CABIN_ZONES) if args.get("zone") == "all" else args.get("zones") or ([args["zone"]] if args.get("zone") else [])
            remaining = [zone for zone in targets if zone not in draft.excluded_zones]
            if not remaining:
                return None, "Bạn nói rõ các vị trí muốn thực hiện nhé."
            args.pop("zone", None)
            args["zones"] = remaining
        validated, error = validate_tool_arguments(self.name, args)
        if validated and self.name == "memory.remember":
            from pydantic import ValidationError

            from src.vivi.memory.sqlite import MemoryValue
            key = validated["memory_key"]
            try:
                MemoryValue(key=key, kind="command" if key.startswith("command.") else "note" if key.startswith("note.") else "fact", value=validated["memory_value"])
            except ValidationError:
                return None, "Thông tin ghi nhớ chưa hợp lệ. Nhiệt độ phải từ 16 đến 30 độ C; bạn nói lại nội dung nhé."
        return validated, error


REGISTRY = {}
for name in TOOL_REGISTRY:
    fields = frozenset(TOOL_REGISTRY[name].parameters.model_fields)
    transferable = fields.intersection({"zone", "zones", "open"})
    executor = "memory" if name.startswith("memory.") else "handbook" if name == "manual.search" else "navigation" if name == "navigation.search" else "vehicle"
    REGISTRY[name] = Capability(name, executor, transferable)


def catalog(navigation=False):
    return [cap.spec.prompt_spec() for cap in REGISTRY.values() if navigation or cap.executor != "navigation"]


def transferable_arguments(old, new):
    source, target = REGISTRY.get(old.capability), REGISTRY.get(new.capability)
    compatible = source.transferable & target.transferable if source and target else frozenset()
    return {key: val for key, val in old.arguments.model_dump(exclude_none=True).items() if key in compatible}
