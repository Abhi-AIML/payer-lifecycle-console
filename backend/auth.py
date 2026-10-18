"""Authentication and sessions for the Payer Lifecycle Console. In-memory (no database yet), so accounts and sessions reset with the server.

Copied from the MaatriLink build, not imported, so each product deploys alone.

- Passwords: salted PBKDF2-HMAC-SHA256, constant-time comparison, no plaintext stored.
- Sessions: random 256-bit tokens kept server-side, sent as an HttpOnly cookie. Absolute and idle expiry.
- Lockout: repeated failures lock that username for a short time. Trade-off: an attacker can lock a user out.
- Unknown usernames and wrong passwords give the same answer and take the same time.

DEMO ONLY: every demo account shares one password. Never reuse it for anything real.
On Cloud Run it can be replaced with DEMO_PASSWORD from Secret Manager (see deploy/setup.sh) and handed to judges privately.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

import config  # noqa: F401  (loads .env before DEMO_PASSWORD is read)

ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256
DEFAULT_DEMO_PASSWORD = "MaatriDemo#1"
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD") or DEFAULT_DEMO_PASSWORD
MAX_FAILS, LOCK_SECONDS = 5, 60
ABSOLUTE_TTL, IDLE_TTL = 8 * 3600, 3600
ROLES = ("benefits_analyst", "claims_analyst", "manager")

_LOCK = threading.RLock()
USERS: dict[str, dict] = {}
SESSIONS: dict[str, dict] = {}
FAILS: dict[str, dict] = {}
_DUMMY_SALT = secrets.token_bytes(16)


class LockedOut(Exception):
    def __init__(self, seconds: int):
        super().__init__(f"Too many failed attempts. Try again in {seconds} seconds.")
        self.seconds = seconds


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)


def reset_users() -> None:
    with _LOCK:
        USERS.clear()
        FAILS.clear()


def add_user(username: str, name: str, role: str, password: str, **extra) -> dict:
    if role not in ROLES:
        raise ValueError(f"Unknown role {role}")
    salt = secrets.token_bytes(16)
    user = {"username": username.lower(), "name": name, "role": role, "salt": salt, "hash": _hash(password, salt),
            "title": extra.get("title", "")}
    with _LOCK:
        USERS[user["username"]] = user
    return user


def public(user: dict) -> dict:
    return {k: user[k] for k in ("username", "name", "role", "title")}


def authenticate(username: str, password: str) -> dict | None:
    """Returns the user, None on a bad login, or raises LockedOut."""
    key = (username or "").strip().lower()
    now = time.time()
    with _LOCK:
        f = FAILS.get(key)
        if f and f["locked_until"] > now:
            raise LockedOut(int(f["locked_until"] - now) + 1)
        user = USERS.get(key)
    expected = user["hash"] if user else _hash("dummy", _DUMMY_SALT)
    supplied = _hash(password or "", user["salt"] if user else _DUMMY_SALT)
    ok = hmac.compare_digest(expected, supplied) and user is not None
    with _LOCK:
        if ok:
            FAILS.pop(key, None)
            return user
        f = FAILS.setdefault(key, {"count": 0, "locked_until": 0})
        f["count"] += 1
        if f["count"] >= MAX_FAILS:
            f.update(count=0, locked_until=now + LOCK_SECONDS)
    return None


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        SESSIONS[token] = {"username": username, "created": now, "last_seen": now}
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _LOCK:
        s = SESSIONS.get(token)
        if not s:
            return None
        if now - s["created"] > ABSOLUTE_TTL or now - s["last_seen"] > IDLE_TTL or s["username"] not in USERS:
            SESSIONS.pop(token, None)
            return None
        s["last_seen"] = now
        return USERS[s["username"]]


def destroy_session(token: str | None) -> None:
    with _LOCK:
        SESSIONS.pop(token or "", None)
