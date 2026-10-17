"""Spoken-response regressions from logs 962–965; POI/energy data stay intact."""

import json
from dataclasses import replace

import pytest

from src.vivi.navigation.energy import EnergyPlanner
from src.vivi.navigation.presentation import choice_message, locality, place_label, result_messages, short_name
from src.vivi.navigation.vietmap import Place, Route
from tests.support.dialogue_v2 import ScriptedModel, build, invoke
from tests.support.long_term_memory import services as services
from tests.support.navigation_workflow import HANOI, STOP, IntercityMaps, profile, progress, station


class LoggedCoffeeMaps:
    def __init__(self):
        self.searches = 0

    async def search(self, text, **kwargs):
        self.searches += 1
        return [
            Place(
                ref_id="habakuk",
                name="Quán Cà Phê Habakuk Ngõ Phan Huy Chú",
                address="Phường Cửa Nam, Thành phố Hà Nội",
                lat=21,
                lng=105.86,
            ),
            Place(
                ref_id="nguoi-mot-nha",
                name="Quán Cà Phê Người Một Nhà Ngõ 183 Đặng Tiến Đông",
                address="Phường Đống Đa, Thành phố Hà Nội",
                lat=21,
                lng=105.87,
            ),
            Place(
                ref_id="hai-tam-hon",
                name="Quán Cà Phê Hai Tâm Hồn Phố Mai Anh Tuấn",
                address="Phường Ô Chợ Dừa, Thành phố Hà Nội",
                lat=21,
                lng=105.88,
            ),
        ]

    async def route(self, a, b):
        return Route(distance_m=3200, duration_s=360, coordinates=[[a[1], a[0]], [b[1], b[0]]], steps=[])


@pytest.mark.parametrize(
    "address,expected",
    [
        ("12 Phan Huy Chú, Phường Cửa Nam, TP. Hà Nội", "Phường Cửa Nam"),
        ("Xã An Khánh, Thành phố Hà Nội", "Xã An Khánh"),
        ("Thị trấn TEST, Tỉnh TEST", "Thị trấn TEST"),
        ("TP Hà Nội", ""),
        ("Điểm đại diện mô phỏng", ""),
        ("", ""),
    ],
)
def test_read_locality_without_city(address, expected):
    assert locality(address) == expected


def test_short_names_preserve_brand_and_distinguish_branches():
    assert short_name("Quán Cà Phê Phố Cổ") == "Quán Cà Phê Phố Cổ"
    assert short_name("Quán Cà Phê Phố Cổ Ngõ TEST") == "Quán Cà Phê Phố Cổ"
    assert (
        place_label({"name": "Nhà Thuốc Phường Cửa Nam", "address": "Phường Cửa Nam, TP Hà Nội"})
        == "Nhà Thuốc Phường Cửa Nam"
    )
    message = choice_message(
        [
            {"name": "Nhà Thuốc An Ngõ Một", "address": "Phường TEST, Thành phố Hà Nội"},
            {"name": "Nhà Thuốc An Ngõ Hai", "address": "Phường TEST, Thành phố Hà Nội"},
        ]
    )
    assert "Ngõ Một" in message and "Ngõ Hai" in message
    assert "Thành phố" not in message


def choices(handles, message):
    return {
        "message": message,
        "ui_events": [{"type": "navigation.results"}],
        "navigation_update": {"candidates": [{"handle": handle} for handle in handles]},
    }


def test_duplicate_choice_lists_use_identity_and_keep_other_step_results():
    result = result_messages(
        [
            choices(["a", "b"], "Tìm thấy A và B"),
            {"message": "Đã chỉnh 24 độ"},
            choices(["a", "b"], "A, B. Bạn chọn số mấy?"),
        ]
    )
    assert result == ["Đã chỉnh 24 độ", "A, B. Bạn chọn số mấy?"]
    # A new search with new handles and genuinely repeated effects are retained.
    assert result_messages([choices(["a"], "A"), choices(["new-a"], "A")]) == ["A", "A"]
    assert result_messages([{"message": "Đã bật nhạc"}, {"message": "Đã bật nhạc"}]) == ["Đã bật nhạc"] * 2


@pytest.mark.parametrize("tier", ["rules", "google", "local"])
@pytest.mark.asyncio
async def test_log_962_compound_turn_speaks_choices_once_and_retains_full_data(services, tmp_path, tier):
    maps = LoggedCoffeeMaps()
    model = ScriptedModel(
        [
            {
                "act": "request",
                "actions": [
                    {"capability": "navigation.search", "arguments": {"query": "quán cà phê", "nearest": True}},
                    {"capability": "climate.set_temperature", "arguments": {"value_celsius": 24}},
                    {"capability": "navigation.plan", "arguments": {"use_search_results": True}},
                ],
            }
        ]
    )
    services = replace(services, navigation=maps, energy_planner=EnergyPlanner(profile()))
    graph, _, gateway = build(
        services, tmp_path, model if tier == "google" else None, local=model if tier == "local" else None
    )
    graph.default_provider = tier
    try:
        result = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "962",
                "input_text": "tìm quán cà phê gần, đặt điều hòa 24 độ rồi dẫn đường",
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            }
        )
        assert result["diagnostics"]["effective_provider"] == tier
        text = result["response_text"]
        for name in ("Habakuk", "Người Một Nhà", "Hai Tâm Hồn"):
            assert text.count(name) == 1
        assert text.count("Bạn chọn chỗ nào?") == 1
        assert "Phường Cửa Nam" in text
        assert "Thành phố Hà Nội" not in text and "Phan Huy Chú" not in text
        assert text.index("24 độ") < text.index("Habakuk")
        assert result["tts_text"] == text
        assert maps.searches == 1 and gateway.vehicle.state_for("driver").temperature_celsius == 24
        assert [step["status"] for step in result["steps"]] == ["completed", "completed", "waiting_slot"]
        assert result["navigation"]["candidates"][0]["name"] == "Quán Cà Phê Habakuk Ngõ Phan Huy Chú"
        assert result["navigation"]["candidates"][0]["address"] == "Phường Cửa Nam, Thành phố Hà Nội"
        assert result["navigation"]["candidates"][0]["spoken_label"] == "Quán Cà Phê Habakuk, Phường Cửa Nam"
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_log_963_preview_and_start_use_short_speech_with_precise_energy(services, tmp_path):
    services = replace(services, navigation=LoggedCoffeeMaps(), energy_planner=EnergyPlanner(profile()))
    graph, _, gateway = build(services, tmp_path)
    gateway.vehicle.set_energy("driver", 82, 246)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "search",
                "input_text": "dẫn đường đến quán cà phê gần nhất",
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            }
        )
        result = await invoke(graph, "chỗ đầu tiên", "choose")
        text = result["response_text"]
        assert "Habakuk, Phường Cửa Nam" in text
        assert "Phan Huy Chú" not in text and "Thành phố Hà Nội" not in text
        # Short speech leaves the arrival charge to the screen; the summary keeps it.
        assert result["navigation"]["summary"]["arrival_soc"] == 80 and "mô phỏng" not in text
        plan = result["navigation"]["energy"]["selected_plan"]
        assert plan["arrival_soc"] == pytest.approx(80.93, abs=0.01) and plan["simulated"]
        assert result["status"] == "answered" and not gateway.vehicle.state_for("driver").driving
        assert result["navigation"]["offer"]["status"] == "pending"
        # Log 964: the start button answers the pending offer itself, so it starts at
        # once with the energy plan that was shown; no second question.
        started = await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "964",
                "input_text": "Bắt đầu hành trình",
                "action": {
                    "capability": "navigation.start",
                    "arguments": {"route_revision": result["navigation"]["revision"]},
                },
            }
        )
        assert started["response_text"] == "Đã bắt đầu dẫn đường."
        assert started["navigation"]["status"] == "running" and gateway.vehicle.state_for("driver").driving
        assert started["navigation"]["energy"]["selected_plan"] == plan
        assert not started["navigation"].get("offer")
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_charging_speech_rounds_minutes_but_preserves_calculation(services, tmp_path):
    services = replace(
        services,
        navigation=IntercityMaps(),
        energy_planner=EnergyPlanner(profile(), [station(lat=STOP[0], lng=STOP[1])]),
    )
    services.action_gateway.vehicle.set_energy("driver", 40, 120)
    graph, _, _ = build(services, tmp_path)
    try:
        await graph.ainvoke(
            {
                "session_id": "driver",
                "turn_id": "search",
                # A named place: the synthetic map has no verified centre for the region.
                "input_text": "chỉ đường tới Hải Phòng TEST",
                "position": {"lat": HANOI[0], "lng": HANOI[1], "simulated": False},
            }
        )
        result = await invoke(graph, "chỗ đầu tiên", "choose")
        plan = result["navigation"]["energy"]["selected_plan"]
        assert plan["stops"][0]["charge_minutes"] == pytest.approx(10.4)
        assert "dừng sạc 1 lần" in result["response_text"]
        assert "11 phút" not in result["response_text"]
        started = await invoke(graph, "đồng ý", "start")
        assert "trạm sạc thứ 1" in started["response_text"]
        arrived = await progress(graph, started["navigation"], "to-station", kind="progress", distance_m=50000)
        assert "khoảng 11 phút" in arrived["response_text"]
        assert "10.4" not in result["response_text"] and "mô phỏng" not in result["response_text"]
        assert plan["simulated"] and result["navigation"]["energy"]["simulated"]
    finally:
        graph.close()


@pytest.mark.asyncio
async def test_api_stream_does_not_repeat_choices_between_speech_and_final(services, tmp_path, client, monkeypatch):
    from src.vivi.api.runtime import runtime
    from src.vivi.config import Settings
    from src.vivi.orchestration import LangGraphOrchestrator
    from src.vivi.persistence.event_store import EventStore

    services = replace(services, navigation=LoggedCoffeeMaps(), energy_planner=EnergyPlanner(profile()))
    graph, ledger, gateway = build(services, tmp_path)
    store = EventStore(Settings(data_dir=tmp_path / "api", store_transcripts=False, store_audio=False))
    monkeypatch.setattr(runtime, "orchestrator", LangGraphOrchestrator({"rules": graph}, gateway, store, ledger=ledger))
    monkeypatch.setattr(runtime, "store", store)
    try:
        response = await client.post(
            "/api/v1/turn/stream",
            json={
                "session_id": "driver",
                "turn_id": "stream-962",
                "llm_provider": "rules",
                "transcript": "tìm quán cà phê gần, đặt điều hòa 24 độ rồi dẫn đường",
                "position": {"lat": 21, "lng": 105.85, "simulated": False},
            },
        )
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        spoken = " ".join(event["text"] for event in events if event["type"] == "speech")
        final = next(event["response"] for event in events if event["type"] == "final")
        assert spoken == final["message"] and spoken.count("Habakuk") == 1
        assert final["navigation"]["candidates"][0]["address"] == "Phường Cửa Nam, Thành phố Hà Nội"
    finally:
        graph.close()
