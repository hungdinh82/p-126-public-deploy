"""Geometry references for telemetry; authoritative snapshots remain complete."""


def compact_snapshot(payload, known_routes):
    def compact(value):
        if isinstance(value, list):
            return [compact(item) for item in value]
        if not isinstance(value, dict):
            return value
        route_id = value.get("route_id")
        referenced = route_id and value.get("route") and route_id in known_routes
        # Skip cached geometry before traversing it: telemetry must not allocate
        # copies of a long route just to discard them from its response.
        result = {
            key: compact(item)
            for key, item in value.items()
            if not (referenced and key in {"route", "waypoint_routes"})
        }
        if referenced:
            result["route_reference"] = route_id
            # Waypoint shapes are immutable calculation inputs, already cached
            # with this route. Energy/SOC/alerts remain present and current.
        return result

    return compact(payload)
