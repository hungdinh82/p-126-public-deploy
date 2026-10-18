from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
PIN_RE = re.compile(r"^[0-9]{4,12}$")


@dataclass(frozen=True)
class Principal:
    account_id: str
    phone: str
    role: str


class SessionStore:
    """Own account authentication, login sessions, and conversation ownership."""

    def __init__(self, path: Path, *, session_hours: int = 8) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_seconds = session_hours * 60 * 60
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    phone TEXT NOT NULL UNIQUE,
                    pin_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('driver', 'engineer')),
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    phone TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL,
                    locked_until REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_sessions_account_idx ON auth_sessions(account_id);
                CREATE INDEX IF NOT EXISTS conversations_account_idx ON conversations(account_id, last_active_at);
                """
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def normalize_phone(value: str) -> str:
        phone = re.sub(r"[\s().-]", "", value.strip())
        if phone.startswith("0"):
            phone = "+84" + phone[1:]
        elif phone.startswith("84"):
            phone = "+" + phone
        if not PHONE_RE.fullmatch(phone):
            raise ValueError("Số điện thoại không hợp lệ.")
        return phone

    @staticmethod
    def _pin_hash(pin: str, salt: bytes | None = None) -> str:
        if not PIN_RE.fullmatch(pin):
            raise ValueError("PIN phải gồm 4 đến 12 chữ số.")
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()

    @classmethod
    def _verify_pin(cls, pin: str, encoded: str) -> bool:
        try:
            algorithm, salt_value, expected_value = encoded.split("$", 2)
            if algorithm != "scrypt":
                return False
            salt = base64.urlsafe_b64decode(salt_value.encode())
            actual = cls._pin_hash(pin, salt).split("$", 2)[2]
            return hmac.compare_digest(actual, expected_value)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def provision_engineer(self, phone: str, pin: str) -> None:
        if not phone and not pin:
            return
        if not phone or not pin:
            raise ValueError("VIVI_ENGINEER_PHONE và VIVI_ENGINEER_PIN phải được cấu hình cùng nhau.")
        normalized = self.normalize_phone(phone)
        encoded = self._pin_hash(pin)
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT id FROM accounts WHERE phone=?", (normalized,)).fetchone()
            if row:
                db.execute(
                    "UPDATE accounts SET pin_hash=?, role='engineer', status='active' WHERE id=?", (encoded, row["id"])
                )
            else:
                db.execute(
                    "INSERT INTO accounts(id,phone,pin_hash,role,status,created_at) VALUES (?,?,?,'engineer','active',?)",
                    (str(uuid4()), normalized, encoded, now),
                )

    def login(self, phone: str, pin: str) -> tuple[str, Principal]:
        normalized = self.normalize_phone(phone)
        if not PIN_RE.fullmatch(pin):
            raise ValueError("Số điện thoại hoặc PIN không đúng.")
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute(
                "SELECT failures,locked_until FROM login_attempts WHERE phone=?", (normalized,)
            ).fetchone()
            if attempt and attempt["locked_until"] > now:
                raise ValueError("Số điện thoại hoặc PIN không đúng. Hãy thử lại sau.")
            row = db.execute("SELECT * FROM accounts WHERE phone=?", (normalized,)).fetchone()
            if row is None:
                account_id = str(uuid4())
                db.execute(
                    "INSERT INTO accounts(id,phone,pin_hash,role,status,created_at) VALUES (?,?,?,'driver','active',?)",
                    (account_id, normalized, self._pin_hash(pin), now),
                )
                role = "driver"
            else:
                if row["status"] != "active" or not self._verify_pin(pin, row["pin_hash"]):
                    failures = (attempt["failures"] if attempt else 0) + 1
                    locked_until = now + 300 if failures >= 5 else 0
                    db.execute(
                        """INSERT INTO login_attempts(phone,failures,locked_until) VALUES (?,?,?)
                           ON CONFLICT(phone) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until""",
                        (normalized, failures, locked_until),
                    )
                    db.commit()
                    raise ValueError("Số điện thoại hoặc PIN không đúng.")
                account_id, role = row["id"], row["role"]
            db.execute("DELETE FROM login_attempts WHERE phone=?", (normalized,))
            token = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO auth_sessions(token_hash,account_id,expires_at,last_seen_at) VALUES (?,?,?,?)",
                (self._token_hash(token), account_id, now + self.session_seconds, now),
            )
        return token, Principal(account_id=account_id, phone=normalized, role=role)

    def principal(self, token: str | None) -> Principal | None:
        if not token:
            return None
        now = time.time()
        key = self._token_hash(token)
        with self.connect() as db:
            row = db.execute(
                """SELECT a.id, a.phone, a.role, a.status, s.expires_at
                   FROM auth_sessions s JOIN accounts a ON a.id=s.account_id
                   WHERE s.token_hash=?""",
                (key,),
            ).fetchone()
            if row is None or row["status"] != "active" or row["expires_at"] <= now:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (key,))
                return None
            db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?", (now, key))
        return Principal(account_id=row["id"], phone=row["phone"], role=row["role"])

    def logout(self, token: str | None) -> None:
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._token_hash(token),))

    def create_conversation(self, account_id: str) -> str:
        identifier, now = str(uuid4()), time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversations(id,account_id,status,created_at,last_active_at) VALUES (?,?,'active',?,?)",
                (identifier, account_id, now, now),
            )
        return identifier

    def owns_conversation(self, account_id: str, conversation_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND account_id=? AND status='active'",
                (conversation_id, account_id),
            ).fetchone()
            if row:
                db.execute("UPDATE conversations SET last_active_at=? WHERE id=?", (time.time(), conversation_id))
        return row is not None
