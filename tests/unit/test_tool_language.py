"""Localized wire labels preserve separate tools and their argument schemas."""

from src.vivi.dialogue.tool_language import localized_tools


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


def test_wire_labels_keep_separate_tools_and_roundtrip_identity():
    original = tools("open_door", "close_door", "lock_door", "unlock_door")
    wire, reverse = localized_tools(original)
    assert len(wire) == len(original) == 4
    assert [reverse[t["function"]["name"]] for t in wire] == [t["function"]["name"] for t in original]
    assert all(t["function"]["parameters"] == original[i]["function"]["parameters"] for i, t in enumerate(wire))
    assert len({t["function"]["name"] for t in wire}) == 4
