"""Navigation ownership, ordering, crash recovery and public transport contracts."""

import asyncio
from threading import Event

import pytest

from src.vivi.navigation.transport import compact_snapshot
from src.vivi.persistence.checkpoints import ThreadedSqliteSaver
from tests.support.long_term_memory import services as services
from tests.support.navigation_lifecycle import event


async def ordered(graph, gear, identifier, sequence, client="tab-1", **extra):
    snapshot = await graph.snapshot("driver")
    return await event(
        graph,
        snapshot,
        "gear",
        identifier,
        gear=gear,
        client_id=client,
        control_sequence=sequence,
        runtime_epoch=graph.runtime_epoch,
        **extra,
    )


@pytest.mark.asyncio
async def test_checkpoint_close_waits_for_cancelled_worker_without_closing_connection(services, tmp_path):
    saver = ThreadedSqliteSaver(tmp_path / "worker.sqlite3")
    entered, release = Event(), Event()

    def blocked_read():
        entered.set()
        release.wait(5)
        return saver.connection.execute("SELECT 1").fetchone()[0]

    task = asyncio.create_task(saver._run(blocked_read))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    closing = asyncio.create_task(saver.aclose())
    await asyncio.sleep(0.01)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 2)
    await saver.aclose()  # Idempotent teardown.


def test_transport_preserves_current_soc_and_omits_only_known_geometry():
    route = {"coordinates": [[105, 21], [106, 21]], "distance_m": 1000, "duration_s": 10, "steps": []}
    payload = {
        "navigation": {"route_id": "r1", "route": route, "waypoint_routes": [route], "simulated_soc": 49},
        "navigation_workspace": {"journey": {"route_id": "r1", "route": route}},
        "trip_plan": None,
    }
    compact = compact_snapshot(payload, {"r1"})
    assert "route" not in compact["navigation"]
    assert compact["navigation"]["route_reference"] == "r1"
    assert compact["navigation"]["simulated_soc"] == 49
    assert compact_snapshot(payload, set()) == payload
    assert payload["navigation"]["route"] == route
