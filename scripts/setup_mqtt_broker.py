"""Create loopback-only Mosquitto credentials and ACL for the local demo."""

from __future__ import annotations

import argparse
import secrets
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/mqtt"))
    parser.add_argument("--port", type=int, default=1883)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    passwd_tool = shutil.which("mosquitto_passwd")
    if not passwd_tool:
        parser.error("mosquitto_passwd is required (install Mosquitto first)")
    directory = args.data_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    credentials = directory / "credentials.env"
    password_file = directory / "passwords"
    acl_file = directory / "acl"
    config_file = directory / "mosquitto.conf"
    if any(path.exists() for path in (credentials, password_file, acl_file, config_file)):
        parser.error(f"MQTT configuration already exists in {directory}; move it aside before regenerating")

    api_password = secrets.token_urlsafe(32)
    simulator_password = secrets.token_urlsafe(32)
    subprocess.run([passwd_tool, "-b", "-c", str(password_file), "vivi-api", api_password], check=True)
    subprocess.run([passwd_tool, "-b", str(password_file), "vivi-simulator", simulator_password], check=True)
    acl_file.write_text(
        "user vivi-api\n"
        "topic write vivi/v1/vehicles/+/commands\n"
        "topic read vivi/v1/vehicles/+/acks\n"
        "topic read vivi/v1/vehicles/+/state\n"
        "topic read vivi/v1/vehicles/+/availability\n"
        "user vivi-simulator\n"
        "topic read vivi/v1/vehicles/+/commands\n"
        "topic write vivi/v1/vehicles/+/acks\n"
        "topic write vivi/v1/vehicles/+/state\n"
        "topic write vivi/v1/vehicles/+/availability\n",
        encoding="utf-8",
    )
    config_file.write_text(
        f"listener {args.port} 127.0.0.1\n"
        "allow_anonymous false\n"
        f"password_file {password_file}\n"
        f"acl_file {acl_file}\n"
        "persistence false\n",
        encoding="utf-8",
    )
    credentials.write_text(
        "VIVI_VEHICLE_PROVIDER=mqtt\n"
        "MQTT_HOST=127.0.0.1\n"
        f"MQTT_PORT={args.port}\n"
        "MQTT_API_USERNAME=vivi-api\n"
        f"MQTT_API_PASSWORD={api_password}\n"
        "MQTT_SIM_USERNAME=vivi-simulator\n"
        f"MQTT_SIM_PASSWORD={simulator_password}\n",
        encoding="utf-8",
    )
    for path in (credentials, password_file, acl_file, config_file):
        path.chmod(0o600)
    print(f"MQTT broker configuration created at {config_file}")
    print("Credentials are stored in ignored data/mqtt/credentials.env; do not commit them.")


if __name__ == "__main__":
    main()
