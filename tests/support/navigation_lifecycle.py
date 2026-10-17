"""Shared component fixtures extracted from historical scenario modules."""

from dataclasses import replace

from src.vivi.navigation.energy import EnergyPlanner
from tests.support.dialogue_v2 import build, invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_refinement import DestinationMaps
from tests.support.navigation_workflow import profile


async def start(graph):
    await graph.ainvoke(
        {"session_id": "driver", "turn_id": "find", "input_text": "đi Tràng An", "position": {"lat": 21, "lng": 105.85}}
    )
    await invoke(graph, "chỗ đầu tiên", "choose")
    return await invoke(graph, "đồng ý", "start")


async def event(graph, result, kind, identifier, **extra):
    return await graph.ainvoke(
        {
            "session_id": "driver",
            "turn_id": identifier,
            "navigation_event": {"kind": kind, "id": identifier, "revision": result["navigation"]["revision"], **extra},
        }
    )


def runtime(services, tmp_path, **kwargs):
    return build(
        replace(services, navigation=DestinationMaps(), energy_planner=EnergyPlanner(profile())), tmp_path, **kwargs
    )
