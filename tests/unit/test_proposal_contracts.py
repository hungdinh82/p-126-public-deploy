import pytest

from src.vivi.dialogue.proposal_contracts import (
    continuation_stop_tools,
    permitted_tools,
    validate_operation_choices,
)


def tools(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
        for name in names
    ]


@pytest.mark.parametrize(
    ("text", "wrong", "right"),
    [
        ("mở khóa cửa sau bên phải", "lock_door", "unlock_door"),
        ("dong cua phia ghe phu", "open_door", "close_door"),
        ("nâng kính sau trái", "lower_window", "raise_window"),
        ("hạ kính phía tài xế", "raise_window", "lower_window"),
        ("giảm điều hòa bớt 4 độ", "increase_temperature", "decrease_temperature"),
        ("tắt nhạc giúp bác", "play_music", "pause_music"),
        ("độ mở kính hiện tại", "lower_window", "get_window_positions"),
    ],
)
def test_opposite_or_mutating_proposals_are_rejected(text, wrong, right):
    declared = tools(wrong, right)
    with pytest.raises(ValueError, match="trái"):
        validate_operation_choices([(wrong, {})], declared, text)
    validate_operation_choices([(right, {})], declared, text)


def test_mixed_read_and_write_preserves_both_requested_operations():
    declared = tools("open_door", "close_door", "get_door_status", "get_battery_level")
    names = {t["function"]["name"] for t in permitted_tools(declared, "mở cửa sau trái rồi đọc pin")}
    assert {"open_door", "get_battery_level"} <= names
    assert not {"close_door", "get_door_status"} & names


def test_two_explicit_directions_remain_representable():
    declared = tools("open_door", "close_door")
    validate_operation_choices(
        [("open_door", {}), ("close_door", {})], declared, "mở cửa bên phụ rồi đóng cửa sau trái"
    )


def test_amount_for_one_window_does_not_remove_other_window_endpoint():
    declared = tools("lower_window", "raise_window", "set_window")
    names = {t["function"]["name"] for t in permitted_tools(declared, "hạ kính bên tài 20% rồi nâng kính sau phải")}
    assert {"set_window", "raise_window"} <= names


def test_stop_continuation_can_never_start_music():
    capabilities = [{"name": "media.play"}, {"name": "media.pause"}]
    assert continuation_stop_tools("đừng phát nhạc nữa nhé", capabilities) == {"pause_music"}
    assert continuation_stop_tools("đừng mở cửa nữa", [{"name": "door.set_open"}]) is None
