"""Verify the deployed ViVi core flow against its public HTTP contract."""

from __future__ import annotations

import argparse
from uuid import uuid4

import httpx


def _turn(client: httpx.Client, base_url: str, session_id: str, provider: str, text: str) -> dict:
    response = client.post(
        f"{base_url}/api/v1/turn",
        json={
            "transcript": text,
            "session_id": session_id,
            "turn_id": str(uuid4()),
            "llm_provider": provider,
        },
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--provider", choices=("rules", "local", "openai", "google", "openrouter"), default="rules")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")
    session_id = f"smoke-{uuid4()}"

    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(f"{base_url}/api/v1/health")
        health.raise_for_status()
        health_payload = health.json()
        available = {
            item["provider"]: item["available"] for item in health_payload["llm"]["options"]
        }
        if not available.get(args.provider):
            raise RuntimeError(f"LLM provider is not configured: {args.provider}")
        if not health_payload["orchestration"].get("handbook_available"):
            raise RuntimeError("handbook SQLite artifact is missing; import it before smoke testing")

        handbook = _turn(
            client,
            base_url,
            session_id,
            args.provider,
            "Hướng dẫn sạc pin VF8 như thế nào?",
        )
        if handbook["route"] != "handbook" or handbook["status"] != "verified" or not handbook["evidence"]:
            raise RuntimeError(f"handbook flow failed: {handbook}")

        climate = _turn(client, base_url, session_id, args.provider, "Đặt nhiệt độ 25 độ")
        if climate["status"] != "verified" or climate["vehicle_state"]["temperature_celsius"] != 25:
            raise RuntimeError(f"vehicle verification flow failed: {climate}")

        sensitive = _turn(client, base_url, session_id, args.provider, "Mở cửa sổ bên tài")
        if sensitive["status"] != "confirmation_required" or not sensitive["confirmation"]:
            raise RuntimeError(f"confirmation proposal flow failed: {sensitive}")
        confirmation_id = sensitive["confirmation"]["confirmation_id"]
        approved = client.post(
            f"{base_url}/api/v1/confirmations/{confirmation_id}",
            json={
                "session_id": session_id,
                "turn_id": str(uuid4()),
                "decision": "approve",
                "llm_provider": args.provider,
            },
        )
        approved.raise_for_status()
        approval_payload = approved.json()
        if (
            approval_payload["status"] != "verified"
            or approval_payload["vehicle_state"]["window_driver_percent"] != 100
        ):
            raise RuntimeError(f"confirmation execution flow failed: {approval_payload}")

    print(
        "ViVi core smoke passed: health -> handbook+citation -> verified action -> "
        "confirmation -> verified sensitive action"
    )


if __name__ == "__main__":
    main()
