"""Opt-in VietMap read-only smoke: real POIs/routes, chosen test SOC, no car/DB."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from src.vivi.config import Settings
from src.vivi.dialogue.contracts import NavigationState
from src.vivi.navigation.charging import VietMapChargingDiscovery
from src.vivi.navigation.energy import EnergyPlanner, EnergyProfile, load_assumptions, load_stations
from src.vivi.navigation.vietmap import VietMapClient
from src.vivi.navigation.workflow import NavigationWorkflow


async def smoke(args):
    config = Settings()
    maps = VietMapClient(config)
    if not maps.available:
        raise ValueError("VIETMAP_API_KEY is not configured")
    origin = (config.navigation_default_lat, config.navigation_default_lng)
    choices = await maps.search(args.destination, near=origin)
    if not choices:
        raise ValueError("No destination POI returned")
    selected = (
        next((p for p in choices if p.ref_id == args.destination_ref), None) if args.destination_ref else choices[0]
    )
    if selected is None:
        raise ValueError("Destination ref is not in the search results")
    detail = await maps.place(selected.ref_id)
    selected = selected.model_copy(update={"lat": detail.lat, "lng": detail.lng})
    route = await maps.route(origin, (selected.lat, selected.lng))
    profile = EnergyProfile.model_validate_json(config.energy_profile_path.read_text())
    equipment = load_stations(config.charging_stations_path)
    provider = VietMapChargingDiscovery(
        maps,
        equipment,
        category=config.navigation_charging_category,
        radius_m=config.navigation_charging_radius_m,
        budget_seconds=config.navigation_charging_search_seconds,
        max_detour_m=config.navigation_charging_max_detour_m,
        assumptions=load_assumptions(config.charging_assumptions_path, config.charging_assumptions_enabled),
    )
    workflow = NavigationWorkflow(
        SimpleNamespace(navigation=maps, energy_planner=EnergyPlanner(profile, equipment), charging_discovery=provider)
    )
    nav = NavigationState(
        origin={"lat": origin[0], "lng": origin[1]}, selected=selected.model_dump(), route=route.model_dump()
    )
    started = time.perf_counter()
    energy, plans = await workflow.plan_energy(nav, args.soc, "rules")
    return {
        "origin": {"lat": origin[0], "lng": origin[1], "label": config.navigation_default_label},
        "destination": selected.model_dump(),
        "route": route.model_dump(),
        "test_soc": args.soc,
        "energy": energy,
        "plans": plans,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "limitations": [
            "Map POIs and roads are live; SOC is an explicit smoke input, not real telemetry.",
            "No car action, navigation start, model call or conversation DB write.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default="Vincom Plaza Thanh Hóa")
    parser.add_argument("--destination-ref", default=None)
    parser.add_argument("--soc", type=float, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.soc <= 100:
        parser.error("SOC must be between 0 and 100")
    result = asyncio.run(smoke(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["energy"]["status"],
                "origin": result["origin"]["label"],
                "destination": result["destination"]["name"],
                "route_km": round(result["route"]["distance_m"] / 1000, 1),
                "stations": [
                    {
                        "name": s["name"],
                        "rest_area": s["rest_area_name"],
                        "road": s["road_context"],
                        "distance_km": round(s["distance_m"] / 1000, 1),
                        "arrival_soc": s["arrival_soc"],
                        "power_kw": s["power_kw"],
                    }
                    for s in result["energy"].get("station_candidates", [])
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
