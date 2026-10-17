import pytest

from tests.support.dialogue_v2 import invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_lifecycle import event, runtime


@pytest.mark.asyncio
async def test_workspace_consent(services, tmp_path):
    graph, _, _ = runtime(services, tmp_path)
    try:
        for i, text in enumerate(["đi Tràng An", "chỗ đầu tiên", "đồng ý"]):
            r = await graph.ainvoke(
                {"session_id": "driver", "turn_id": str(i), "input_text": text, "position": {"lat": 21, "lng": 105.85}}
            )
        assert r["navigation"]["status"] == "running"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_pending_replacement_consent_after_pause_and_resume(services, tmp_path):
    from tests.support.navigation_lifecycle import start

    graph, _, _ = runtime(services, tmp_path)
    try:
        old = await start(graph)
        offered = await event(graph, old, "point", "new", position={"lat": 21, "lng": 106, "label": "Vinhomes"})
        paused = await event(graph, offered, "pause", "p")
        await event(graph, paused, "resume", "d")
        accepted = await invoke(graph, "đồng ý", "accept")
        assert accepted["navigation"]["selected"]["name"] == "Vinhomes"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_workspace_separates_draft_and_journey_and_records_terminal_history(services, tmp_path):
    from tests.support.navigation_lifecycle import start

    graph, _, gateway = runtime(services, tmp_path)
    try:
        running = await start(graph)
        offered = await event(
            graph, running, "point", "new-place", position={"lat": 21, "lng": 106, "label": "Vinhomes"}
        )
        workspace = offered["session"]["navigation_workspace"]
        assert "navigation" not in offered["session"]
        assert workspace["journey"]["route_id"] == running["navigation"]["route_id"]
        assert workspace["draft"]["selected"]["name"] == "Vinhomes"
        assert "progress_m" not in workspace["draft"]
        ended = await event(graph, offered, "end", "end")
        assert ended["navigation"]["status"] == "ended"
        assert not gateway.vehicle.state_for("driver").driving
        assert len(ended["session"]["navigation_workspace"]["history"]) == 1
        cleared = await event(graph, ended, "history_clear", "clear-history")
        assert cleared["session"]["navigation_workspace"]["history"] == []
        assert cleared["session"]["navigation_workspace"]["draft"]["selected"]["name"] == "Vinhomes"
    finally:
        graph.close()
