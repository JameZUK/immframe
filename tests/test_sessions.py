from __future__ import annotations

from pathlib import Path

from immframe.sessions import (
    COOKIE_NAME, MAX_FAILURES, SHORT_SESSION_S, SessionManager, clear_cookie_header,
    cookie_header, load_secret,
)

SECRET = b"s" * 32


def test_issue_and_verify_round_trip():
    m = SessionManager("james", "pw", days=30, secret=SECRET)
    token, max_age = m.issue(True, now=1000)
    assert max_age == 30 * 86400
    s = m.verify(token, now=1001)
    assert s is not None and s.user == "james" and s.remember and s.expires == 1000 + 30 * 86400


def test_short_session_without_remember():
    m = SessionManager("james", "pw", secret=SECRET)
    token, max_age = m.issue(False, now=0)
    assert max_age is None
    assert m.verify(token, now=SHORT_SESSION_S - 1) is not None
    assert m.verify(token, now=SHORT_SESSION_S + 1) is None


def test_tampering_and_expiry_rejected():
    m = SessionManager("james", "pw", secret=SECRET)
    token, _ = m.issue(True, now=0)
    parts = token.split(".")
    longer = ".".join(parts[:2] + [str(int(parts[2]) + 10**9)] + parts[3:])
    assert m.verify(longer, now=1) is None                          # extended expiry, bad sig
    assert m.verify(token[:-2] + "xx", now=1) is None
    assert m.verify("garbage", now=1) is None and m.verify(None) is None and m.verify("") is None
    assert m.verify(token, now=10**9) is None                       # expired


def test_password_change_invalidates_sessions():
    old = SessionManager("james", "old", secret=SECRET)
    token, _ = old.issue(True)
    assert SessionManager("james", "new", secret=SECRET).verify(token) is None
    assert SessionManager("other", "old", secret=SECRET).verify(token) is None
    assert SessionManager("james", "old", secret=SECRET).verify(token) is not None


def test_renewal_after_half_life_for_remembered_only():
    m = SessionManager("james", "pw", days=10, secret=SECRET)
    token, _ = m.issue(True, now=0)
    s = m.verify(token, now=1)
    assert not m.needs_renewal(s, now=1)
    assert m.needs_renewal(s, now=6 * 86400)
    short = m.verify(m.issue(False, now=0)[0], now=1)
    assert not m.needs_renewal(short, now=SHORT_SESSION_S - 10)


def test_credentials_and_lockout():
    m = SessionManager("james", "pw", secret=SECRET)
    assert m.check_credentials("james", "pw")
    assert not m.check_credentials("james", "nope") and not m.check_credentials("x", "pw")
    for _ in range(MAX_FAILURES - 1):
        m.record_failure("1.2.3.4")
    assert not m.locked_out("1.2.3.4")
    m.record_failure("1.2.3.4")
    assert m.locked_out("1.2.3.4") and not m.locked_out("5.6.7.8")
    m.clear_failures("1.2.3.4")
    assert not m.locked_out("1.2.3.4")


def test_secret_persists_with_0600(tmp_path: Path):
    path = tmp_path / "k" / "session.key"
    a = load_secret(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_secret(path) == a and len(a) == 32


def test_sessions_survive_restart_via_persisted_secret():
    token, _ = SessionManager("james", "pw").issue(True)            # default secret path (tmp via conftest)
    assert SessionManager("james", "pw").verify(token) is not None


def test_cookie_headers():
    h = cookie_header("tok", 60)
    assert h.startswith(f"{COOKIE_NAME}=tok") and "HttpOnly" in h and "SameSite=Lax" in h and "Max-Age=60" in h
    assert "Max-Age" not in cookie_header("tok", None)
    assert "Max-Age=0" in clear_cookie_header()
