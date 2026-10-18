from src.vivi.capabilities.execution import CapabilityRuntime


def test_tire_status_uses_vietnamese_position_labels() -> None:
    message = CapabilityRuntime.status_message(
        {
            "temperature_celsius": 23,
            "battery_percent": 82,
            "range_km": 328,
            "tire_pressures_kpa": {
                "front_left": 245,
                "front_right": 246,
                "rear_left": 247,
                "rear_right": 248,
            },
            "hood_open": False,
            "trunk_open": False,
            "door_states": {},
        },
        "tires",
    )

    assert message == (
        "Áp suất lốp trước trái là 245 kPa. Áp suất lốp trước phải là 246 kPa. "
        "Áp suất lốp sau trái là 247 kPa. Áp suất lốp sau phải là 248 kPa."
    )
    assert "front_left" not in message
