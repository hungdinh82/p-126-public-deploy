"""Durable navigation state and immutable event receipts, outside graph checkpoints."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from src.vivi.navigation.projections import dialogue_result
from src.vivi.persistence.runtime_store import fingerprint
from src.vivi.turn_control import record_io

DOMAIN_FIELDS = {
    "snapshot_sequence",
    "control_revision",
    "navigation_workspace",
    "navigation_focus",
    "control_receipts",
}


def domain_payload(session):
    return session.model_dump(mode="json", include=DOMAIN_FIELDS)


def checkpoint_session(session):
    value = session.model_dump(mode="json", exclude=DOMAIN_FIELDS)
    for task in [value.get("active"), *value.get("suspended", [])]:
        if task:
            for step in task.get("steps", []):
                if step.get("result"):
                    step["result"] = dialogue_result(step["result"])
    value["navigation_reference"] = {"revision": session.navigation_workspace.revision}
    return value


class NavigationRepository:
    def __init__(self, ledger):
        self.ledger = ledger
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vivi-navigation")
        self._lifecycle = threading.Lock()
        self._closing = False
        self._close_future = None
        with ledger.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS navigation_routes (route TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS navigation_sessions (
                    session TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS navigation_receipts (
                    session TEXT NOT NULL, event TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL, receipt TEXT, PRIMARY KEY(session,event));
                CREATE TABLE IF NOT EXISTS navigation_clients (
                    session TEXT NOT NULL, client TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event TEXT NOT NULL, PRIMARY KEY(session,client));
            """)

    async def run(self, function, *args):
        with self._lifecycle:
            if self._closing:
                raise RuntimeError("Navigation store is closing")
            future = self._executor.submit(function, *args)
        started = time.perf_counter()
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        finally:
            record_io("navigation_store", started)

    def close(self):
        with self._lifecycle:
            if not self._closing:
                self._closing = True
                self._close_future = self._executor.submit(lambda: None)
                self._executor.shutdown(wait=False)

    async def aclose(self):
        self.close()
        await asyncio.shield(asyncio.wrap_future(self._close_future))

    def load(self, key):
        payload = self.load_payload(key)
        return json.loads(payload) if payload is not None else None

    def load_payload(self, key):
        with self.ledger.connect() as db:
            row = db.execute("SELECT payload FROM navigation_sessions WHERE session=?", (key,)).fetchone()
            if not row:
                return None
            payload = json.loads(row[0])
            for owner in [
                payload.get("navigation_workspace", {}).get("draft"),
                payload.get("navigation_workspace", {}).get("journey"),
            ]:
                if owner and owner.pop("calculation_reference", None):
                    geometry = db.execute(
                        "SELECT payload FROM navigation_routes WHERE route=?", (owner["route_id"],)
                    ).fetchone()
                    if not geometry:
                        raise RuntimeError("Missing immutable route calculation")
                    owner.update(json.loads(geometry[0]))
            return json.dumps(payload, ensure_ascii=False)

    def save(self, key, session, receipt=None):
        full = domain_payload(session)
        payload = json.dumps(full, ensure_ascii=False)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for owner in [full["navigation_workspace"].get("draft"), full["navigation_workspace"].get("journey")]:
                if owner and owner.get("route_id") and owner.get("route"):
                    calculation = {
                        field: owner.pop(field, None)
                        for field in ("route", "waypoint_routes", "energy", "alternatives")
                    }
                    encoded = json.dumps(calculation, ensure_ascii=False, sort_keys=True)
                    known = db.execute(
                        "SELECT payload FROM navigation_routes WHERE route=?", (owner["route_id"],)
                    ).fetchone()
                    if known and known[0] != encoded:
                        raise ValueError("An immutable route ID cannot identify a different calculation")
                    db.execute("INSERT OR IGNORE INTO navigation_routes VALUES (?,?)", (owner["route_id"], encoded))
                    owner["calculation_reference"] = owner["route_id"]
            db.execute(
                "INSERT INTO navigation_sessions VALUES (?,?) ON CONFLICT(session) DO UPDATE SET payload=excluded.payload",
                (key, json.dumps(full, ensure_ascii=False)),
            )
            if receipt:
                db.execute(
                    "UPDATE navigation_receipts SET status=?,receipt=? WHERE session=? AND event=?",
                    (receipt["status"], json.dumps(receipt, ensure_ascii=False), key, receipt["event_id"]),
                )
        return payload

    def receipt(self, key, identifier):
        with self.ledger.connect() as db:
            row = db.execute(
                "SELECT status,receipt FROM navigation_receipts WHERE session=? AND event=?", (key, identifier)
            ).fetchone()
        if not row:
            return None
        return (
            json.loads(row["receipt"])
            if row["receipt"]
            else {"event_id": identifier, "status": "unknown", "error_code": "interrupted_event"}
        )

    def reserve(self, key, value, control_revision=0):
        identity = dict(value)
        if identity.get("position"):
            # Position supplies a server observation timestamp by default. A
            # retry with the same coordinates must not collide on that clock.
            identity["position"] = {k: v for k, v in identity["position"].items() if k != "observed_at"}
        digest = fingerprint(identity)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM navigation_receipts WHERE session=? AND event=?", (key, value["id"])
            ).fetchone()
            if row:
                if row["fingerprint"] != digest:
                    raise ValueError("event_id đã được dùng cho một yêu cầu khác.")
                return (
                    json.loads(row["receipt"])
                    if row["receipt"]
                    else {"event_id": value["id"], "status": "unknown", "error_code": "interrupted_event"}
                )
            reserved = {
                "event_id": value["id"],
                "kind": value["kind"],
                "gear": value.get("gear"),
                "status": "unknown",
                "error_code": "interrupted_event",
                "control_revision": control_revision,
            }
            db.execute(
                "INSERT INTO navigation_receipts VALUES (?,?,?,'unknown',?)",
                (key, value["id"], digest, json.dumps(reserved)),
            )
        return None

    def uncertain_control(self, key, control_revision):
        with self.ledger.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM navigation_receipts WHERE session=? AND status='unknown' "
                    "AND json_extract(receipt, '$.kind') IN ('gear','pause','resume','end','clear') "
                    "AND COALESCE(json_extract(receipt, '$.control_revision'),0)>=? LIMIT 1",
                    (key, control_revision),
                ).fetchone()
                is not None
            )

    def has_drive_receipt(self, key):
        with self.ledger.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM navigation_receipts WHERE session=? AND json_extract(receipt, '$.gear')='D' AND status IN ('completed','unknown') LIMIT 1",
                    (key,),
                ).fetchone()
                is not None
            )

    def accept_sequence(self, key, value):
        if value.get("control_sequence") is None:
            return True
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence,event FROM navigation_clients WHERE session=? AND client=?", (key, value["client_id"])
            ).fetchone()
            if row and (
                value["control_sequence"] < row["sequence"]
                or value["control_sequence"] == row["sequence"]
                and value["id"] != row["event"]
            ):
                return False
            db.execute(
                "INSERT INTO navigation_clients VALUES (?,?,?,?) ON CONFLICT(session,client) DO UPDATE SET sequence=excluded.sequence,event=excluded.event",
                (key, value["client_id"], value["control_sequence"], value["id"]),
            )
        return True

    def reset_park(self, key, *, complete=False):
        with self.ledger.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_navigation_resets'").fetchone():
                return False
            if complete:
                db.execute("UPDATE runtime_navigation_resets SET park=0 WHERE session=?", (key,))
                return False
            row = db.execute("SELECT park FROM runtime_navigation_resets WHERE session=?", (key,)).fetchone()
            return bool(row and row[0])

    def migrate(self, saver):
        # Active legacy sessions are deliberately not resumed. Runtime migration
        # backs them up before resetting navigation and invalidating old grants.
        return None
