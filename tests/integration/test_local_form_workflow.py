"""Proposal → persisted question → consent → effect, using current Core runtime."""

import json
from copy import deepcopy

import pytest

from src.vivi.dialogue.local_operations import CONTRACT_VERSION
from src.vivi.domain.models import VehicleState
from tests.support.dialogue_v2 import build, invoke
from tests.support.long_term_memory import services as services


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "name", "arguments", "path", "initial", "expected"),
    [
        ("mở cửa sau phải", "open_door", {"targets": ["rear_right"]}, "door_states.rear_right.open", False, True),
        ("đóng cửa sau phải", "close_door", {"targets": ["rear_right"]}, "door_states.rear_right.open", True, False),
        ("khóa cửa sau phải", "lock_door", {"targets": ["rear_right"]}, "door_states.rear_right.locked", False, True),
        (
            "mở khóa cửa sau phải",
            "unlock_door",
            {"targets": ["rear_right"]},
            "door_states.rear_right.locked",
            True,
            False,
        ),
        ("nâng kính sau phải", "raise_window", {"targets": ["rear_right"]}, "window_positions.rear_right", 60, 0),
        ("hạ kính sau phải", "lower_window", {"targets": ["rear_right"]}, "window_positions.rear_right", 60, 100),
        ("mở capo", "open_hood", {}, "hood_open", False, True),
        ("đóng capo", "close_hood", {}, "hood_open", True, False),
        ("mở cốp", "open_trunk", {}, "trunk_open", False, True),
        ("đóng cốp", "close_trunk", {}, "trunk_open", True, False),
        ("mở tất cả cửa nắp capo và cốp", "open_body", {}, "body", False, True),
        ("đóng tất cả cửa nắp capo và cốp", "close_body", {}, "body", True, False),
    ],
)
async def test_operation_polarity_reaches_vehicle_state(
    services, tmp_path, text, name, arguments, path, initial, expected
):
    """An independent state oracle catches an inverted adapter or actuator."""

    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"
        calls = 0

        async def agenerate_proposals(self, **kwargs):
            self.calls += 1
            assert name in {t["function"]["name"] for t in kwargs["tools"]}
            return [(name, arguments)]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = "local"
    paths = (
        [
            "hood_open",
            "trunk_open",
            *[f"door_states.{z}.open" for z in ("driver", "front_passenger", "rear_left", "rear_right")],
        ]
        if path == "body"
        else [path]
    )
    fixture = gateway.vehicle.state_for("driver").model_dump()
    for state_path in paths:
        target = fixture
        keys = state_path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = initial
    gateway.vehicle.apply_shared_fixture(VehicleState.model_validate(fixture))
    try:
        result = await invoke(graph, text, "propose")
        if result["output"]["confirmation"]:
            result = await invoke(graph, "ok", "approve")
        state = gateway.vehicle.state_for("driver").model_dump()
        assert result["output"]["status"] == "action_verified"
        for state_path in paths:
            value = state
            for key in state_path.split("."):
                value = value[key]
            assert value == expected
        # Changing a panel or its lock never moves the window or the other
        # property; window commands never move a door.
        for key in ("door_states", "window_positions", "hood_open", "trunk_open"):
            for leaf, value in flatten_state(fixture[key], key).items():
                if leaf not in paths:
                    assert flatten_state(state[key], key)[leaf] == value
        assert model.calls == 1
    finally:
        await graph.aclose()


def flatten_state(value, prefix):
    if not isinstance(value, dict):
        return {prefix: value}
    return {leaf: val for key, item in value.items() for leaf, val in flatten_state(item, prefix + "." + key).items()}


@pytest.mark.asyncio
@pytest.mark.parametrize(("answers", "zone"), [
    ([("trước trái", "driver")], "driver"),
    ([("đằng trước trái", "driver")], "driver"),
    ([("ở đằng trước phía bên tay phải", "front_passenger")], "front_passenger"),
    ([("cửa trước", "front"), ("bên trái", "left")], "driver"),
    ([("bên phải", "right"), ("đằng sau", "rear")], "rear_right"),
    ([("phía sau bên trái nhé", "rear_left")], "rear_left"),
])
async def test_short_positions_persist_both_coordinates_before_consent(services, tmp_path, answers, zone):
    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"
        calls = 0

        async def agenerate_proposals(self, *, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return [("open_door", {"targets": []})]
            assert {t["function"]["name"] for t in tools} == {"update_current_request"}
            value = answers[self.calls - 2][1]
            schema = tools[0]["function"]["parameters"]["properties"]["targets"]
            assert value in schema["items"]["enum"]
            return [("update_current_request", {"targets": [value]})]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = "local"
    initial = gateway.vehicle.state_for("driver").model_dump()
    try:
        result = await invoke(graph, "mở cửa", "start")
        step_id = result["steps"][0]["id"]
        assert result["steps"][0]["status"] == "waiting_slot"
        for index, (text, _) in enumerate(answers):
            result = await invoke(graph, text, f"position-{index}")
            assert result["steps"][0]["id"] == step_id
            assert gateway.vehicle.state_for("driver").model_dump() == initial
        step = result["steps"][0]
        assert step["status"] == "waiting_confirmation"
        assert step["arguments"]["open"] is True
        coordinates = {
            ("front", "left"): "driver", ("front", "right"): "front_passenger",
            ("rear", "left"): "rear_left", ("rear", "right"): "rear_right",
        }
        stored_zone = step["arguments"].get("zone") or coordinates.get(
            (step["arguments"].get("target_row"), step["arguments"].get("target_side"))
        )
        assert stored_zone == zone
        assert step["result"]["confirmation"]["action"]["arguments"]["zone"] == zone
        result = await invoke(graph, "đúng rồi", "confirm")
        assert result["output"]["status"] == "action_verified"
        after = gateway.vehicle.state_for("driver").model_dump()
        expected = deepcopy(initial)
        expected["door_states"][zone]["open"] = True
        expected["door_driver_open"] = zone == "driver"
        assert after.pop("state_version") == expected.pop("state_version") + 1
        assert after.pop("updated_at") >= expected.pop("updated_at")
        assert after == expected
        assert model.calls == len(answers) + 1
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_partial_model_block_prevents_music_effect_and_repair(services, tmp_path):
    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"
        calls = 0

        async def agenerate_proposals(self, **kwargs):
            self.calls += 1
            return [("pause_music", {}), ("dialogue_clarify", {})]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = "local"
    state = gateway.vehicle.state_for("driver")
    state.media_playing = True
    gateway.vehicle.apply_shared_fixture(state)
    try:
        result = await invoke(graph, "tạm dừng nhạc và bật đèn pha", "partial-block")
        assert result["output"]["status"] == "clarify"
        assert gateway.vehicle.state_for("driver").media_playing
        assert model.calls == 1
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_new_same_operation_cannot_approve_or_rewrite_previous_form(services, tmp_path):
    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"
        calls = 0

        async def agenerate_proposals(self, **kwargs):
            self.calls += 1
            assert "open_door" in {t["function"]["name"] for t in kwargs["tools"]}
            return [("open_door", {"targets": ["driver" if self.calls == 1 else "rear_right"]})]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = "local"
    try:
        first = await invoke(graph, "mở cửa ghế lái", "first")
        assert first["output"]["confirmation"]
        second = await invoke(graph, "mở cửa sau phải", "second")
        assert second["output"]["confirmation"]
        assert not gateway.vehicle.state_for("driver").door_states.rear_right.open
        await invoke(graph, "ok", "approve-second")
        doors = gateway.vehicle.state_for("driver").door_states
        assert doors.rear_right.open and not doors.driver.open
        assert model.calls == 2
    finally:
        await graph.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["ok", "oke", "okee", "OKEEE!", "ô kê nhé", "đồng ý ạ"])
async def test_model_fills_location_then_literal_ok_executes_once(services, tmp_path, approval):
    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"
        calls = 0

        async def agenerate_proposals(self, **kwargs):
            self.calls += 1
            context = json.loads(kwargs["user"].split("\n", 1)[1].split("\nLỜI NGƯỜI DÙNG HIỆN TẠI:", 1)[0])
            # Telemetry is loaded for host-side "remaining" resolution only.
            assert "vehicle_observation" not in context
            if self.calls == 1:
                return [("open_door", {"targets": []})]
            assert context["form"]["operation"] == "door_open"
            return [
                (
                    "update_current_request",
                    {
                        "targets": ["rear_right"],
                    },
                )
            ]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = "local"
    try:
        first = await invoke(graph, "mở cửa", "create")
        assert first["output"]["diagnostics"]["prompt_version"] == CONTRACT_VERSION
        assert first["steps"][0]["status"] == "waiting_slot"
        assert all(not d["open"] for d in gateway.vehicle.state_for("driver").door_states.model_dump().values())
        second = await invoke(graph, "sau phải", "update")
        assert second["output"]["confirmation"]
        third = await invoke(graph, approval, "approve")
        assert gateway.vehicle.state_for("driver").door_states.rear_right.open
        assert third["output"]["diagnostics"]["semantic_source"] == "literal_question_reply"
        assert model.calls == 2
        await invoke(graph, "ok", "old-ok")
        assert model.calls >= 2
    finally:
        await graph.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "calls", "included", "excluded"), [
    (
        "pin còn lại và áp suất lốp bao nhiêu",
        [("get_battery_level", {}), ("get_tire_pressure", {})],
        ["Pin còn", "Áp suất lốp"], ["Điều hoà", "Nắp capo", "Các cửa:"],
    ),
    (
        "trạng thái cửa và kính xe",
        [("get_door_status", {}), ("get_window_positions", {})],
        ["Các cửa:", "Độ mở cửa kính:"], ["Pin còn", "Nắp capo", "Cốp sau"],
    ),
    (
        "nhiệt độ cabin và trạng thái cốp",
        [("get_cabin_temperature", {}), ("get_trunk_status", {})],
        ["Điều hoà", "Cốp sau"], ["Pin còn", "Nắp capo", "Các cửa:"],
    ),
])
async def test_multiple_status_tools_return_only_requested_measurements(services, tmp_path, text, calls, included, excluded):
    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"

        async def agenerate_proposals(self, **kwargs):
            names = {t["function"]["name"] for t in kwargs["tools"]}
            assert {name for name, _ in calls} <= names
            assert "vehicle_get_status" not in names
            return calls

    graph, _, gateway = build(services, tmp_path, local=Model())
    graph.default_provider = "local"
    initial = gateway.vehicle.state_for("driver").model_dump()
    try:
        result = await invoke(graph, text, "read-multiple")
        output = result["output"]
        assert output["status"] == "action_verified"
        assert all(part in output["response_text"] for part in included)
        assert all(part not in output["response_text"] for part in excluded)
        assert len(result["steps"]) == 1
        assert gateway.vehicle.state_for("driver").model_dump() == initial
    finally:
        await graph.aclose()


@pytest.mark.asyncio
async def test_unrelated_request_cannot_become_model_invented_consent(services, tmp_path):
    class Model:
        tools_as_json = True
        local_semantic_contract = "operations_v1"
        calls = 0

        async def agenerate_proposals(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return [("open_door", {"targets": ["driver"]})]
            return [("dialogue_answer", {"option_id": "yes"})]

    model = Model()
    graph, _, gateway = build(services, tmp_path, local=model)
    graph.default_provider = "local"
    try:
        first = await invoke(graph, "mở cửa bên tài", "create")
        assert first["output"]["confirmation"]
        result = await invoke(graph, "bật nhạc", "new-command")
        assert result["output"]["status"] == "clarify"
        assert not gateway.vehicle.state_for("driver").door_states.driver.open
        assert model.calls == 3  # Initial proposal, one invalid answer, one repair.
    finally:
        await graph.aclose()
