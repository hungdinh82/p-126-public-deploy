"""Capability catalog and parameter boundary; contains no language interpretation."""

from __future__ import annotations

from dataclasses import dataclass

from src.vivi.agents.tools import TOOL_REGISTRY, validate_tool_arguments
from src.vivi.dialogue.contracts import ActionDraft
from src.vivi.vehicle.zones import CABIN_ZONES

DEFAULT_CLIMATE_DELTA_CELSIUS = 2.0


@dataclass(frozen=True)
class Capability:
    name: str
    executor: str
    transferable: frozenset[str] = frozenset()
    exclusive_arguments: tuple[frozenset[str], ...] = ()

    @property
    def spec(self):
        return TOOL_REGISTRY[self.name]

    def validate(self, draft: ActionDraft, observation=None):
        args = draft.arguments.model_dump(exclude_none=True)
        side, row = args.pop("target_side", None), args.pop("target_row", None)
        if (side or row) and not args.get("zone") and not args.get("zones"):
            if not row:
                return None, "Bạn muốn phía trước hay phía sau?"
            if not side:
                return None, "Bạn muốn bên trái hay bên phải?"
            args["zone"] = {
                ("front", "left"): "driver",
                ("front", "right"): "front_passenger",
                ("rear", "left"): "rear_left",
                ("rear", "right"): "rear_right",
            }[row, side]
        if (
            self.name == "manual.search"
            and args.get("question_kind") == "charging_duration"
            and not args.get("charging_mode")
        ):
            return None, "Bạn muốn hỏi thời gian sạc AC hay sạc nhanh DC?"
        direction = args.pop("temperature_direction", None)
        delta = args.pop("temperature_delta_celsius", None)
        if direction is not None and delta is None and args.get("value_celsius") is not None:
            # Answering "how many degrees" with a number below the cabin range
            # ("tăng thêm 3 độ") is a change, not a target of 3°C.
            answered = args["value_celsius"]
            if answered < 16:
                delta = answered if direction == "increase" else -answered
                args.pop("value_celsius")
        if direction is not None and args.get("value_celsius") is None and delta is None:
            # A comfort complaint without a number: ask how many degrees to
            # change, keeping the spoken direction, rather than guessing a step.
            verb = "tăng thêm" if direction == "increase" else "giảm bớt"
            return None, f"Bạn muốn {verb} bao nhiêu độ?"
        if delta is not None:
            if self.name != "climate.set_temperature" or "value_celsius" in args:
                return None, "Bạn nói rõ nhiệt độ muốn đặt nhé."
            if observation is None:
                return None, "Mình chưa đọc được nhiệt độ hiện tại."
            args["value_celsius"] = min(30, max(16, observation.temperature_celsius + delta))
        if draft.excluded_zones:
            if not self.transferable.intersection({"zone", "zones"}):
                return None, "Thao tác này không hỗ trợ loại trừ vị trí."
            targets = (
                list(CABIN_ZONES)
                if args.get("zone") == "all"
                else args.get("zones") or ([args["zone"]] if args.get("zone") else [])
            )
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
                MemoryValue(
                    key=key,
                    kind="command" if key.startswith("command.") else "note" if key.startswith("note.") else "fact",
                    value=validated["memory_value"],
                )
            except ValidationError:
                return None, "Thông tin ghi nhớ chưa hợp lệ. Nhiệt độ phải từ 16 đến 30 độ C; bạn nói lại nội dung nhé."
        return validated, error

    def missing_slots(self, draft):
        args = draft.arguments
        if self.transferable.intersection({"zone", "zones"}) and not (args.zone or args.zones):
            return (
                ["target_row"]
                if args.target_side and not args.target_row
                else ["target_side"]
                if args.target_row and not args.target_side
                else ["zone"]
            )
        if (
            self.name == "climate.set_temperature"
            and args.value_celsius is None
            and args.temperature_delta_celsius is None
        ):
            return ["value_celsius"]
        if self.name == "manual.search" and args.question_kind == "charging_duration" and not args.charging_mode:
            return ["charging_mode"]
        return [
            key
            for key, field in self.spec.parameters.model_fields.items()
            if field.is_required() and key not in args.model_dump(exclude_none=True)
        ]


REGISTRY = {}
for name in TOOL_REGISTRY:
    fields = frozenset(TOOL_REGISTRY[name].parameters.model_fields)
    transferable = fields.intersection({"zone", "zones", "open"})
    if fields.intersection({"zone", "zones"}):
        transferable |= {"target_side", "target_row"}
    executor = (
        "conversation"
        if name == "conversation.respond"
        else "information"
        if name in {"web.search", "weather.get_forecast", "knowledge.lookup"}
        else "memory"
        if name.startswith("memory.")
        else "handbook"
        if name == "manual.search"
        else "navigation"
        if name.startswith("navigation.")
        else "vehicle"
    )
    exclusive = (
        (frozenset({"value_celsius", "temperature_delta_celsius"}),)
        if name == "climate.set_temperature"
        else (frozenset({"choice_handle", "query", "reject_candidates"}),)
        if name == "navigation.refine"
        else ()
    )
    REGISTRY[name] = Capability(name, executor, transferable, exclusive)


COMPATIBLE_CHANGES = frozenset({("door.set_open", "window.set_position"), ("window.set_position", "door.set_open")})


def compatible_change(old, new):
    return old.capability == new.capability or (old.capability, new.capability) in COMPATIBLE_CHANGES


def merge_exclusions(old, incoming):
    if not REGISTRY[incoming.capability].transferable.intersection({"zone", "zones"}):
        return None
    if incoming.exclusion_mode == "replace":
        return incoming.excluded_zones
    return list(dict.fromkeys([*(old.excluded_zones or []), *(incoming.excluded_zones or [])])) or None


def merge_arguments(old, incoming):
    merged = (
        old.arguments.model_dump(exclude_none=True)
        if old.capability == incoming.capability
        else transferable_arguments(old, incoming)
    )
    updates = incoming.arguments.model_dump(exclude_none=True)
    if incoming.capability.startswith("navigation."):
        if "query" in updates and "destination" not in updates:
            # An absolute query replacement cannot retain a structured name
            # from an earlier slot answer. The executor treats it as authoritative.
            merged.pop("destination", None)
        if "destination" in updates:
            from src.vivi.navigation.contracts import DestinationSpec

            destination = DestinationSpec.model_validate(updates["destination"])
            if destination.selection == "reference":
                merged.pop("query", None)
            else:
                updates["query"] = destination.query()
                if "destination_scope" in REGISTRY[incoming.capability].spec.parameters.model_fields:
                    updates["destination_scope"] = "region" if destination.selection == "region" else "place"
    for group in REGISTRY[incoming.capability].exclusive_arguments:
        if len(group.intersection(updates)) == 1:
            for key in group - updates.keys():
                merged.pop(key, None)
    if "zone" in updates or "zones" in updates:
        for key in ("zone", "zones", "target_side", "target_row"):
            merged.pop(key, None)
    if "target_side" in updates or "target_row" in updates:
        old_zone = merged.pop("zone", None)
        merged.pop("zones", None)
        if old_zone and old_zone != "all":
            merged.setdefault("target_side", "left" if old_zone in {"driver", "rear_left"} else "right")
            merged.setdefault("target_row", "front" if old_zone in {"driver", "front_passenger"} else "rear")
    merged.update(updates)
    return merged


def catalog(navigation=False, *, conversation=True, information=True):
    return [
        cap.spec.prompt_spec()
        for cap in REGISTRY.values()
        if not cap.name.startswith("trip.")
        and (navigation or cap.executor != "navigation" or cap.name == "navigation.set_gear")
        and (conversation or cap.executor != "conversation")
        and (information or cap.executor != "information")
    ]


def transferable_arguments(old, new):
    source, target = REGISTRY.get(old.capability), REGISTRY.get(new.capability)
    compatible = source.transferable & target.transferable if source and target else frozenset()
    values = {key: val for key, val in old.arguments.model_dump(exclude_none=True).items() if key in compatible}
    if old.capability == "door.set_open" and new.capability == "window.set_position" and old.arguments.open is not None:
        values["position_percent"] = 100 if old.arguments.open else 0
    elif (
        old.capability == "window.set_position"
        and new.capability == "door.set_open"
        and old.arguments.position_percent in {0, 100}
    ):
        values["open"] = bool(old.arguments.position_percent)
    return values
