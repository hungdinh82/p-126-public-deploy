"""Dialogue keeps route references/forecasts; the domain owns map geometry."""


def dialogue_navigation(value):
    if isinstance(value, list):
        return [dialogue_navigation(item) for item in value]
    if not isinstance(value, dict):
        return value
    route = "coordinates" in value and "distance_m" in value
    return {
        key: dialogue_navigation(item)
        for key, item in value.items()
        if key not in {"coordinates", "bbox", "legs", "waypoint_routes", "applied_events"}
        and not (route and key == "steps")
    }


def dialogue_result(result):
    return {
        key: dialogue_navigation(value) if key in {"navigation_update", "draft_update", "journey_update"} else value
        for key, value in result.items()
    }


def dialogue_turn(turn):
    value = dict(turn)
    value["results"] = [dialogue_result(result) for result in turn.get("results", [])]
    if turn.get("navigation_result"):
        value["navigation_result"] = dialogue_result(turn["navigation_result"])
    return value
