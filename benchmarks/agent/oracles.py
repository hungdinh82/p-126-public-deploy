"""Provider-independent business decisions, specified without production adapters."""

import unicodedata

READS = {
    "get_cabin_temperature": "temperature", "get_battery_level": "battery",
    "get_tire_pressure": "tires", "get_remaining_range": "range",
    "get_remaining_drive_time": "range_time", "get_body_status": "body",
    "get_door_status": "doors", "get_window_positions": "windows",
    "get_hood_status": "hood", "get_trunk_status": "trunk",
    "get_vehicle_diagnostics": "diagnostics", "get_vehicle_overview": "summary",
}


def read_decisions(field):
    return [{"name": "vehicle.get_status", "arguments": {"query": key}}
            for key in (["battery", "temperature", "body"] if field == "summary" else [field])]


def targets(values):
    if not values:
        return {}
    if values == ["same"]:
        return {"referenced_target": True}
    concrete = {"driver", "front_passenger", "rear_left", "rear_right", "all"}
    if set(values) <= concrete:
        return {"zone": values[0]} if len(values) == 1 else {"zones": values}
    result = {}
    for value in values:
        result["target_side" if value in {"left", "right"} else "target_row"] = value
    return result


def expected_decisions(calls, covers):
    """Compile authored tool intent into a frozen, reviewable semantic oracle."""
    result = []
    for call in calls:
        name, args = call["name"], dict(call.get("arguments", {}))
        if name in READS:
            result.extend(read_decisions(READS[name]))
            continue
        parameters = targets(args.pop("targets", []))
        if name in {"open_door", "close_door", "lock_door", "unlock_door"}:
            opening = name in {"open_door", "lock_door"}
            locked = name in {"lock_door", "unlock_door"}
            name = "door.set_lock" if locked else "door.set_open"
            parameters["locked" if locked else "open"] = opening
        elif name in {"lower_window", "raise_window", "set_window"}:
            parameters["position_percent"] = args.get("percent", 100 if name == "lower_window" else 0)
            name = "window.set_position"
        elif name == "set_temperature":
            parameters["value_celsius"] = args.get("celsius")
            name = "climate.set_temperature"
        elif name in {"increase_temperature", "decrease_temperature"}:
            direction = "increase" if name.startswith("increase") else "decrease"
            amount = args.get("degrees")
            parameters = {"temperature_direction": direction} if amount is None else {
                "temperature_delta_celsius": amount if direction == "increase" else -amount}
            name = "climate.set_temperature"
        elif name.startswith(("open_", "close_")) and name.split("_", 1)[1] in {"hood", "trunk", "body"}:
            part = name.split("_", 1)[1]
            parameters["open"] = name.startswith("open")
            name = part + ".set_open"
        elif name in {"play_music", "pause_music"}:
            name = "media.play" if name == "play_music" else "media.pause"
        elif name.startswith(("get_directions", "calculate_trip")):
            parameters = {"query": args.get("destination"), "purpose": "navigate" if name.startswith("get") else "itinerary",
                          "trip_kind": "round_trip" if "round_trip" in name else "one_way"}
            if "origin" in args:
                parameters["origin_query"] = args["origin"]
            name = "navigation.plan"
        else:
            name = "web.search" if name == "lookup_information" and "web.search" in covers else "knowledge.lookup" if name == "lookup_information" else name.replace("_", ".", 1)
            parameters.update(args)
        result.append({"name": name, "arguments": parameters})
    return result


def actual_decisions(semantic):
    if not semantic:
        return []
    actions = [*semantic.get("actions", []), *[u["action"] for u in semantic.get("step_updates", [])],
               *semantic.get("append_actions", [])]
    result = []
    for action in actions:
        name = action["capability"]
        args = {k: v for k, v in action.get("arguments", {}).items() if v is not None}
        destination = args.get("destination")
        if name in {"navigation.plan", "navigation.search"} and not args.get("query") and isinstance(destination, dict):
            text = destination.get("raw_text") or destination.get("name")
            if text:
                args["query"] = text
        for key in ("plan_id", "route_revision", "destination", "destination_scope"):
            args.pop(key, None)
        if name in {"web.search", "knowledge.lookup", "manual.search", "weather.get_forecast", "conversation.respond"}:
            args.pop("query", None)
        if name == "media.play":
            # The real app only toggles one fixed clip; no song-selection score.
            args.pop("media_query", None)
        if name == "navigation.plan":
            args.setdefault("purpose", "navigate")
            args.setdefault("trip_kind", "one_way")
        if name == "vehicle.get_status":
            for field in args.get("query", "summary").split(","):
                result.extend(read_decisions(field))
        else:
            result.append({"name": name, "arguments": args})
    return result


def decisions_match(actual, expected, *, ordered=False):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    def matches(value, wanted):
        if value["name"] != wanted["name"]:
            return False
        def normalize(arguments):
            args = {k: v for k, v in arguments.items() if v is not None}
            if "zone" not in args and args.get("target_side") and args.get("target_row"):
                coordinates = {("left", "front"): "driver", ("right", "front"): "front_passenger",
                               ("left", "rear"): "rear_left", ("right", "rear"): "rear_right"}
                args["zone"] = coordinates[args.pop("target_side"), args.pop("target_row")]
            if "zones" in args:
                zones = sorted(args["zones"])
                if len(zones) == 1:
                    args["zone"] = zones[0]
                    args.pop("zones")
                elif zones == ["driver", "front_passenger", "rear_left", "rear_right"]:
                    args["zone"] = "all"
                    args.pop("zones")
                else:
                    args["zones"] = zones
            for key in ("query", "location", "origin_query", "memory_value"):
                if isinstance(args.get(key), str):
                    text = unicodedata.normalize("NFD", args[key].casefold()).replace("đ", "d")
                    args[key] = " ".join("".join(c for c in text if unicodedata.category(c) != "Mn").split())
            if wanted["name"] == "navigation.search" and args.get("nearest") is True and isinstance(args.get("query"), str):
                # Distance ranking already has its own required boolean slot.
                # Keep the actual place/category and location constraints exact.
                for phrase in ("gan nhat", "gan day"):
                    args["query"] = args["query"].replace(phrase, " ")
                args["query"] = " ".join(args["query"].split())
            return args
        args, desired = normalize(value["arguments"]), normalize(wanted["arguments"])
        # Navigation/information may carry additional grounded context slots.
        flexible = wanted["name"].startswith("navigation.") or wanted["name"] in {
            "conversation.respond", "web.search", "knowledge.lookup", "manual.search", "weather.get_forecast"}
        if flexible:
            return all(args.get(key) == item for key, item in desired.items())
        return {k: v for k, v in args.items() if v is not None} == {k: v for k, v in desired.items() if v is not None}
    if ordered:
        return all(matches(v, w) for v, w in zip(actual, expected, strict=True))
    def assign(remaining, wanted):
        if not wanted:
            return True
        return any(matches(value, wanted[0]) and assign(remaining[:i] + remaining[i + 1:], wanted[1:])
                   for i, value in enumerate(remaining))
    return assign(actual, expected)
