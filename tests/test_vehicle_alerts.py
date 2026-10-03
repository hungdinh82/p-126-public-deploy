from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.vivi.domain.models import DoorState, DoorStates, TirePressures, VehicleState
from src.vivi.vehicle.alerts import AlertAnnouncementGate, AlertEngine


def state(version: int = 1, **updates) -> VehicleState:
    return VehicleState(
        state_version=version,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=version),
        **updates,
    )


def test_threshold_boundaries_are_not_alerts() -> None:
    snapshot = AlertEngine().evaluate(
        state(
            battery_percent=20,
            tire_pressures_kpa=TirePressures(
                front_left=220, front_right=300, rear_left=250, rear_right=250
            ),
            powertrain_temperature_celsius=90,
        )
    )
    assert snapshot.active_alerts == []


def test_rules_trigger_independent_tire_sources() -> None:
    snapshot = AlertEngine().evaluate(
        state(
            battery_percent=19,
            tire_pressures_kpa=TirePressures(
                front_left=219, front_right=301, rear_left=219, rear_right=250
            ),
            powertrain_temperature_celsius=91,
            power_state="ready",
            door_driver_open=True,
        )
    )
    keys = {(alert.code, alert.source) for alert in snapshot.active_alerts}
    assert keys == {
        ("LOW_BATTERY", "battery"),
        ("TIRE_PRESSURE_LOW", "front_left"),
        ("TIRE_PRESSURE_HIGH", "front_right"),
        ("TIRE_PRESSURE_LOW", "rear_left"),
        ("POWERTRAIN_OVERHEAT", "powertrain"),
        ("DOOR_OPEN_WHEN_READY", "driver"),
    }
    messages = {(alert.code, alert.source): alert.message for alert in snapshot.active_alerts}
    assert messages[("TIRE_PRESSURE_LOW", "front_left")] == (
        "Áp suất lốp trước trái đang thấp, ở mức 219 kilopascal."
    )
    assert messages[("TIRE_PRESSURE_HIGH", "front_right")] == (
        "Áp suất lốp trước phải đang cao, ở mức 301 kilopascal."
    )


def test_alert_episode_is_stable_recovers_and_retriggers_with_new_id() -> None:
    engine = AlertEngine()
    first = engine.evaluate(state(battery_percent=10))
    alert_id = first.active_alerts[0].alert_id
    assert first.event_sequence == 1

    same_version = engine.evaluate(state(battery_percent=10))
    assert same_version.event_sequence == 1
    assert same_version.active_alerts[0].alert_id == alert_id

    still_active = engine.evaluate(state(2, battery_percent=9))
    assert still_active.event_sequence == 1
    assert still_active.active_alerts[0].alert_id == alert_id

    recovered = engine.evaluate(state(3, battery_percent=20))
    assert recovered.event_sequence == 2
    assert recovered.active_alerts == []

    retriggered = engine.evaluate(state(4, battery_percent=19))
    assert retriggered.event_sequence == 3
    assert retriggered.active_alerts[0].alert_id != alert_id


def test_each_open_door_has_an_independent_ready_alert() -> None:
    snapshot = AlertEngine().evaluate(
        state(
            power_state="ready",
            door_states=DoorStates(
                driver=DoorState(open=True),
                rear_right=DoorState(open=True),
            ),
        )
    )
    assert {(alert.code, alert.source) for alert in snapshot.active_alerts} == {
        ("DOOR_OPEN_WHEN_READY", "driver"),
        ("DOOR_OPEN_WHEN_READY", "rear_right"),
    }


def test_announcement_gate_deduplicates_and_only_cools_down_warnings() -> None:
    engine = AlertEngine()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    warning = engine.evaluate(state(battery_percent=10)).active_alerts[0]
    gate = AlertAnnouncementGate(cooldown_seconds=30)
    assert gate.should_announce(warning, now)
    assert not gate.should_announce(warning, now + timedelta(seconds=1))

    engine.evaluate(state(2, battery_percent=20))
    warning_again = engine.evaluate(state(3, battery_percent=10)).active_alerts[0]
    assert not gate.should_announce(warning_again, now + timedelta(seconds=10))
    assert gate.should_announce(warning_again.model_copy(update={"alert_id": "later"}), now + timedelta(seconds=31))

    critical = AlertEngine().evaluate(
        state(4, powertrain_temperature_celsius=100)
    ).active_alerts[0]
    critical_again = critical.model_copy(update={"alert_id": "new-critical"})
    assert gate.should_announce(critical, now)
    assert gate.should_announce(critical_again, now + timedelta(seconds=1))
