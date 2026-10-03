"""Login sessions for the web dashboard.

Replaces the browser's Basic-auth popup with a login form and a signed,
persistent session cookie. Basic auth keeps working for scripts and the
CLI (`immframe state` etc.).

Tokens are stateless and signed: `v1.<user>.<expiry>.<remember>.<sig>`,
HMAC-SHA256 under a key derived from a random secret kept on disk *and*
the configured username + password. Consequences:

- sessions survive restarts (the secret is persisted, 0600, beside the
  hidden list in `$XDG_STATE_HOME/immframe/session.key`);
- changing the password (or username) in config invalidates every
  existing session at once;
- deleting session.key logs everyone out.

"Remember me" sessions last `days` and are renewed when past half-life,
so a phone that opens the dashboard now and then stays logged in
indefinitely. Without it a session lasts 12 hours (browser-session cookie).

Brute force: after `MAX_FAILURES` bad logins from one address within
`LOCKOUT_S`, that address gets 429 until the window passes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .hidden import state_dir

log = logging.getLogger(__name__)

COOKIE_NAME = "immframe_session"
SHORT_SESSION_S = 12 * 3600
MAX_FAILURES = 8
LOCKOUT_S = 15 * 60


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class Session:
    user: str
    expires: int
    remember: bool


def load_secret(path: Path | None = None) -> bytes:
    """The persistent signing secret, created on first use. Falls back to a
    process-lifetime secret (sessions end at restart) if it can't be saved."""
    path = path or (state_dir() / "session.key")
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("session key %s unreadable (%s) — sessions won't survive a restart", path, e)
        return secrets.token_bytes(32)
    secret = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secret)
    except OSError as e:
        log.warning("could not save session key to %s (%s) — sessions won't survive a restart", path, e)
    return secret


class SessionManager:
    def __init__(self, username: str, password: str, *, days: int = 30,
                 secret: bytes | None = None) -> None:
        self._username = username
        self._password = password
        self._days = max(1, int(days))
        base = secret if secret is not None else load_secret()
        self._key = hmac.new(base, f"{username}\0{password}".encode(), hashlib.sha256).digest()
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def long_max_age(self) -> int:
        return self._days * 86400

    # ── Credentials ─────────────────────────────────────────────────────
    def check_credentials(self, username: str, password: str) -> bool:
        ok_user = secrets.compare_digest(username.encode(), self._username.encode())
        ok_pass = secrets.compare_digest(password.encode(), self._password.encode())
        return ok_user and ok_pass

    def locked_out(self, client: str) -> bool:
        with self._lock:
            now = time.time()
            recent = [t for t in self._failures.get(client, []) if now - t < LOCKOUT_S]
            self._failures[client] = recent
            return len(recent) >= MAX_FAILURES

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._failures.setdefault(client, []).append(time.time())

    def clear_failures(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    # ── Tokens ──────────────────────────────────────────────────────────
    def issue(self, remember: bool, *, now: float | None = None) -> tuple[str, int | None]:
        """New token and the cookie Max-Age (None = browser-session cookie)."""
        now = time.time() if now is None else now
        life = self.long_max_age if remember else SHORT_SESSION_S
        payload = f"v1.{_b64(self._username.encode())}.{int(now + life)}.{int(bool(remember))}"
        return f"{payload}.{self._sign(payload)}", (life if remember else None)

    def verify(self, token: str | None, *, now: float | None = None) -> Session | None:
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 5 or parts[0] != "v1":
            return None
        payload, sig = ".".join(parts[:4]), parts[4]
        if not secrets.compare_digest(sig, self._sign(payload)):
            return None
        try:
            user = _unb64(parts[1]).decode()
            expires = int(parts[2])
            remember = parts[3] == "1"
        except (ValueError, UnicodeDecodeError):
            return None
        now = time.time() if now is None else now
        if expires <= now or user != self._username:
            return None
        return Session(user, expires, remember)

    def needs_renewal(self, session: Session, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return session.remember and (session.expires - now) < self.long_max_age / 2

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())


def cookie_header(token: str, max_age: int | None) -> str:
    # SameSite=Lax: sent on top-level navigation (so links into the
    # dashboard work) but never on cross-site POSTs; POSTs are also
    # JSON-only (see http.py's form-content-type guard). No `Secure`
    # flag — the dashboard is served over plain HTTP on the LAN.
    parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
