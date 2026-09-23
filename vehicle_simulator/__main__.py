from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .engine import VehicleSimulator
from .mqtt import MqttVehicleService


def main() -> None:
    load_dotenv()
    load_dotenv("data/mqtt/credentials.env", override=True)
    parser = argparse.ArgumentParser(description="Run the local ViVi Vehicle Simulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--db", type=Path, default=Path("data/vehicle-simulator.sqlite3"))
    parser.add_argument("--enable-test-control", action="store_true")
    parser.add_argument("--mqtt", action="store_true", help="Connect to the MQTT broker")
    parser.add_argument("--mqtt-host", default=os.getenv("MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--vehicle-id", default=os.getenv("VIVI_VEHICLE_ID", "demo-car-1"))
    args = parser.parse_args()
    if args.enable_test_control and args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("test control is only available on loopback")
    simulator = VehicleSimulator(args.db)
    mqtt_service = None
    try:
        if args.mqtt:
            mqtt_service = MqttVehicleService(
                simulator,
                args.vehicle_id,
                host=args.mqtt_host,
                port=args.mqtt_port,
                username=os.getenv("MQTT_SIM_USERNAME") or None,
                password=os.getenv("MQTT_SIM_PASSWORD") or None,
            )
            mqtt_service.start()
        uvicorn.run(
            create_app(args.db, enable_test_control=args.enable_test_control, simulator=simulator),
            host=args.host,
            port=args.port,
        )
    finally:
        if mqtt_service:
            mqtt_service.stop()
        simulator.close()


if __name__ == "__main__":
    main()
