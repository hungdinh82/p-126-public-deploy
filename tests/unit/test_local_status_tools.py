"""Specific vehicle reads preserve requested fields and execution order."""

import pytest

from src.vivi.capabilities.registry import catalog
from src.vivi.dialogue.local_tools import local_tool_documents, proposal_tools, proposals_to_turn


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("get_battery_level", "battery"),
        ("get_tire_pressure", "tires"),
        ("get_cabin_temperature", "temperature"),
        ("get_remaining_range", "range"),
        ("get_remaining_drive_time", "range_time"),
        ("get_body_status", "body"),
        ("get_door_status", "doors"),
        ("get_window_positions", "windows"),
        ("get_hood_status", "hood"),
        ("get_trunk_status", "trunk"),
        ("get_vehicle_diagnostics", "diagnostics"),
        ("get_vehicle_overview", "summary"),
    ],
)
def test_specific_read_is_closed_and_binds_only_its_backend_field(name, field):
    docs = local_tool_documents(catalog())
    text = "kiểm tra xe"
    tools = proposal_tools(docs, {}, text)
    result = proposals_to_turn([(name, {})], docs, {}, text, tools)
    assert result.act == "question"
    assert len(result.actions) == 1
    assert result.actions[0].capability == "vehicle.get_status"
    assert result.actions[0].arguments.query == field
    with pytest.raises(ValueError):
        proposals_to_turn([(name, {"query": "trạng thái cửa mở của xe"})], docs, {}, text, tools)


def test_status_batch_preserves_all_requested_fields_and_actuator_order():
    docs = local_tool_documents(catalog())
    text = "kiểm tra pin và lốp rồi bật nhạc rồi kiểm tra nhiệt độ"
    tools = proposal_tools(docs, {}, text)
    result = proposals_to_turn(
        [
            ("get_battery_level", {}),
            ("get_tire_pressure", {}),
            ("play_music", {}),
            ("get_cabin_temperature", {}),
        ],
        docs,
        {},
        text,
        tools,
    )
    assert [(a.capability, a.arguments.query) for a in result.actions] == [
        ("vehicle.get_status", "battery,tires"),
        ("media.play", None),
        ("vehicle.get_status", "temperature"),
    ]


def test_overview_does_not_drop_an_extra_requested_measurement():
    docs = local_tool_documents(catalog())
    text = "tổng quan xe và áp suất lốp"
    tools = proposal_tools(docs, {}, text)
    result = proposals_to_turn([("get_vehicle_overview", {}), ("get_tire_pressure", {})], docs, {}, text, tools)
    assert result.actions[0].arguments.query == "battery,temperature,body,tires"
