from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from vehicle_simulator.app import create_app
from vehicle_simulator.engine import VehicleSimulator


class VehicleSimulatorServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "vehicle.sqlite3"
        self.client_context = TestClient(create_app(self.database, enable_test_control=True))
        self.client = self.client_context.__enter__()
        self.base = "/api/v1/vehicles/demo-car-1"
        self.control = "/api/v1/test/vehicles/demo-car-1"

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.directory.cleanup()

    def command(self, action, arguments=None, *, key=None, command_id=None, version=None, expires_at=None):
        now = datetime.now(UTC)
        if version is None:
            version = self.client.get(f"{self.base}/state").json()["state_version"]
        return {
            "schema_version": 1,
            "command_id": command_id or str(uuid4()),
            "correlation_id": "test-turn",
            "idempotency_key": key or str(uuid4()),
            "vehicle_id": "demo-car-1",
            "action": action,
            "arguments": arguments or {},
            "issued_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (expires_at or now + timedelta(seconds=5)).isoformat(),
            "expected_state_version": version,
        }

    def post(self, command):
        response = self.client.post(f"{self.base}/commands", json=command)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_catalog_and_versioned_state(self):
        steps = [
            ("climate.set_temperature", {"value_celsius": 24}, "temperature_celsius", 24),
            ("window.set_position", {"position_percent": 40}, "window_driver_percent", 40),
            ("media.play", {}, "media_playing", True),
            ("seat.set_heat_level", {"level": 2}, "seat_driver_heat_level", 2),
            ("door.set_open", {"open": True}, "door_driver_open", True),
            ("door.set_open", {"open": False}, "door_driver_open", False),
            ("door.set_lock", {"locked": True}, "door_driver_locked", True),
        ]
        for number, (action, arguments, field, value) in enumerate(steps, start=1):
            with self.subTest(action=action, arguments=arguments):
                result = self.post(self.command(action, arguments))
                self.assertEqual(result["ack"]["status"], "applied")
                self.assertEqual(result["state"][field], value)
                self.assertEqual(result["state"]["state_version"], number)
                self.assertEqual(self.client.get(f"{self.base}/state").json()[field], value)

        read = self.post(self.command("vehicle.get_state", version=None))
        self.assertEqual(read["state"]["state_version"], len(steps))
        same = self.post(self.command("climate.set_temperature", {"value_celsius": 24}))
        self.assertEqual(same["state"]["state_version"], len(steps))

    def test_idempotency_survives_restart_and_conflicting_payload_is_rejected(self):
        command = self.command("climate.set_temperature", {"value_celsius": 26})
        first = self.post(command)
        self.assertEqual(first["state"]["state_version"], 1)
        self.client_context.__exit__(None, None, None)
        self.client_context = TestClient(create_app(self.database, enable_test_control=True))
        self.client = self.client_context.__enter__()

        duplicate = self.post({**command, "command_id": str(uuid4())})
        self.assertEqual(duplicate["ack"]["status"], "duplicate")
        self.assertEqual(duplicate["ack"]["original_status"], "applied")
        self.assertEqual(duplicate["ack"]["original_command_id"], command["command_id"])
        self.assertEqual(duplicate["state"]["state_version"], 1)

        conflict = self.post({**command, "command_id": str(uuid4()), "arguments": {"value_celsius": 27}})
        self.assertEqual(conflict["ack"]["reason_code"], "idempotency_conflict")
        self.assertEqual(conflict["state"]["temperature_celsius"], 26)

    def test_expired_and_stale_commands_do_not_change_state(self):
        past = datetime.now(UTC) - timedelta(milliseconds=100)
        expired = self.post(self.command("media.play", expires_at=past))
        self.assertEqual(expired["ack"]["reason_code"], "expired")
        self.assertFalse(expired["state"]["media_playing"])

        self.post(self.command("climate.set_temperature", {"value_celsius": 25}))
        stale = self.post(self.command("media.play", version=0))
        self.assertEqual(stale["ack"]["reason_code"], "state_version_conflict")
        self.assertFalse(stale["state"]["media_playing"])

    def test_policy_invariants_are_enforced_inside_simulator(self):
        self.client.put(f"{self.control}/fixture", json={"driving": True})
        blocked_window = self.post(self.command("window.set_position", {"position_percent": 100}))
        self.assertEqual(blocked_window["ack"]["reason_code"], "policy_invariant")
        blocked_door = self.post(self.command("door.set_open", {"open": True}))
        self.assertEqual(blocked_door["ack"]["reason_code"], "policy_invariant")
        blocked_hood = self.post(self.command("hood.set_open", {"open": True}))
        self.assertEqual(blocked_hood["ack"]["reason_code"], "policy_invariant")

        self.client.put(f"{self.control}/fixture", json={"driving": False})
        self.post(self.command("door.set_lock", {"locked": True}))
        locked = self.post(self.command("door.set_open", {"open": True}))
        self.assertEqual(locked["ack"]["reason_code"], "policy_invariant")
        blocked_trunk = self.post(self.command("trunk.set_open", {"open": True}, version=None))
        self.assertIsNone(blocked_trunk["ack"]["reason_code"])
        self.assertTrue(blocked_trunk["state"]["trunk_open"])
        refused = self.client.put(f"{self.control}/fixture", json={"driving": True})
        self.assertNotEqual(refused.status_code, 200)
        locked_all = self.post(self.command("body.set_open", {"open": True}))
        self.assertEqual(locked_all["ack"]["reason_code"], "policy_invariant")
        closed_all = self.post(self.command("body.set_open", {"open": False}))
        self.assertFalse(closed_all["state"]["trunk_open"])
        invalid = self.post(self.command("hood.set_open", {"open": True, "zone": "driver"}))
        self.assertEqual(invalid["ack"]["reason_code"], "invalid_arguments")
        unsupported = self.post(self.command("brake.apply"))
        self.assertEqual(unsupported["ack"]["reason_code"], "unknown_action")

    def test_fault_modes_are_observable_without_false_success(self):
        self.client.put(f"{self.control}/fault", json={"mode": "delay", "delay_ms": 1})
        delayed = self.post(self.command("vehicle.get_state"))
        self.assertEqual(delayed["ack"]["status"], "applied")

        self.client.put(f"{self.control}/fault", json={"mode": "timeout_before_apply"})
        before = self.command("media.play")
        response = self.client.post(f"{self.base}/commands", json=before)
        self.assertEqual(response.status_code, 504)
        self.assertFalse(self.client.get(f"{self.base}/state").json()["media_playing"])

        self.client.put(f"{self.control}/fault", json={"mode": "ack_lost_after_apply"})
        after = self.command("media.play")
        response = self.client.post(f"{self.base}/commands", json=after)
        self.assertEqual(response.status_code, 504)
        self.assertTrue(self.client.get(f"{self.base}/state").json()["media_playing"])
        retry = self.post(after)
        self.assertEqual(retry["ack"]["status"], "duplicate")
        self.assertEqual(retry["ack"]["original_status"], "applied")

        self.client.put(f"{self.control}/fault", json={"mode": "state_mismatch"})
        mismatch = self.post(self.command("seat.set_heat_level", {"level": 3}))
        self.assertEqual(mismatch["ack"]["status"], "applied")
        self.assertEqual(mismatch["state"]["seat_driver_heat_level"], 0)

        self.client.put(f"{self.control}/fault", json={"mode": "reject"})
        rejected = self.post(self.command("climate.set_temperature", {"value_celsius": 20}))
        self.assertEqual(rejected["ack"]["reason_code"], "simulated_reject")

        self.client.put(f"{self.control}/fault", json={"mode": "disconnected"})
        response = self.client.post(f"{self.base}/commands", json=self.command("media.pause"))
        self.assertEqual(response.status_code, 503)

    def test_test_controls_are_disabled_by_default(self):
        with TestClient(create_app(Path(self.directory.name) / "separate.sqlite3")) as client:
            response = client.put(f"{self.control}/fixture", json={"driving": True})
            self.assertEqual(response.status_code, 404)

    def test_invalid_commands_do_not_mutate_state(self):
        missing_identity = self.client.post(f"{self.base}/commands", json={"vehicle_id": "demo-car-1"})
        self.assertEqual(missing_identity.status_code, 422)
        unexpected_field = self.command("media.play")
        unexpected_field["untrusted"] = True
        self.assertEqual(self.client.post(f"{self.base}/commands", json=unexpected_field).status_code, 422)

        missing_version = self.command("media.play")
        missing_version.pop("expected_state_version")
        self.assertEqual(self.post(missing_version)["ack"]["reason_code"], "invalid_arguments")

        bad_range = self.post(self.command("seat.set_heat_level", {"level": 4}))
        self.assertEqual(bad_range["ack"]["reason_code"], "invalid_arguments")
        self.assertEqual(bad_range["state"]["state_version"], 0)

        bad_vehicle = self.command("media.play")
        bad_vehicle["vehicle_id"] = "other-car"
        self.assertEqual(self.client.post(f"{self.base}/commands", json=bad_vehicle).status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/state").json()["state_version"], 0)

    def test_fixture_extends_sensor_state_and_keeps_power_fields_in_sync(self):
        response = self.client.put(
            f"{self.control}/fixture",
            json={
                "power_state": "ready",
                "battery_percent": 15,
                "powertrain_temperature_celsius": 95,
                "tire_pressures_kpa": {
                    "front_left": 210,
                    "front_right": 250,
                    "rear_left": 250,
                    "rear_right": 310,
                },
                "door_driver_open": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["power_state"], "ready")
        self.assertFalse(payload["driving"])
        self.assertEqual(payload["tire_pressures_kpa"]["front_left"], 210)
        self.assertTrue(payload["door_states"]["driver"]["open"])

        driving = self.client.put(f"{self.control}/fixture", json={"driving": True})
        self.assertEqual(driving.status_code, 409)

    def test_legacy_sqlite_state_is_migrated_before_validation(self):
        database = Path(self.directory.name) / "legacy.sqlite3"
        simulator = VehicleSimulator(database)
        legacy = {
            "vehicle_id": "legacy-car",
            "state_version": 7,
            "driving": True,
            "battery_percent": 51,
            "window_driver_percent": 40,
            "door_driver_locked": True,
            "seat_driver_heat_level": 2,
        }
        simulator._db.execute(
            "INSERT INTO vehicles (vehicle_id, state_json) VALUES (?, ?)",
            ("legacy-car", json.dumps(legacy)),
        )
        simulator.close()

        reopened = VehicleSimulator(database)
        try:
            migrated = reopened.get_state("legacy-car")
            self.assertEqual(migrated.power_state, "driving")
            self.assertTrue(migrated.driving)
            self.assertEqual(migrated.tire_pressures_kpa.front_left, 250)
            self.assertEqual(migrated.window_positions.driver, 40)
            self.assertTrue(migrated.door_states.driver.locked)
            self.assertEqual(migrated.seat_heat_levels.driver, 2)
            self.assertEqual(migrated.door_states.front_passenger.open, False)
        finally:
            reopened.close()

    def test_fixture_and_reset_publish_changed_state(self):
        published = []
        simulator = VehicleSimulator(Path(self.directory.name) / "published.sqlite3")
        app = create_app(
            Path(self.directory.name) / "unused.sqlite3",
            enable_test_control=True,
            simulator=simulator,
            publish_state=published.append,
        )
        try:
            with TestClient(app) as client:
                fixture = client.put(
                    "/api/v1/test/vehicles/demo-car-1/fixture",
                    json={"battery_percent": 10},
                )
                reset = client.post("/api/v1/test/vehicles/demo-car-1/reset")
            self.assertEqual(fixture.status_code, 200)
            self.assertEqual(reset.status_code, 200)
            self.assertEqual([item.battery_percent for item in published], [10, 82])
            self.assertGreater(published[1].state_version, published[0].state_version)
        finally:
            simulator.close()

    def test_fixture_rejects_conflicting_legacy_and_structured_door_state(self):
        response = self.client.put(
            f"{self.control}/fixture",
            json={
                "door_driver_open": True,
                "door_states": {"driver": {"open": False, "locked": False}},
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_commands_control_each_cabin_zone_and_all_zones(self):
        passenger_window = self.post(
            self.command(
                "window.set_position",
                {"position_percent": 60, "zone": "front_passenger"},
            )
        )
        self.assertEqual(passenger_window["state"]["window_positions"]["front_passenger"], 60)
        self.assertEqual(passenger_window["state"]["window_positions"]["driver"], 0)

        all_locked = self.post(self.command("door.set_lock", {"locked": True, "zone": "all"}))
        self.assertTrue(all(item["locked"] for item in all_locked["state"]["door_states"].values()))

        rear_heat = self.post(self.command("seat.set_heat_level", {"level": 3, "zone": "rear_right"}))
        self.assertEqual(rear_heat["state"]["seat_heat_levels"]["rear_right"], 3)
        self.assertEqual(rear_heat["state"]["seat_heat_levels"]["driver"], 0)


if __name__ == "__main__":
    unittest.main()
