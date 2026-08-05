"""Audit pass 2, findings 3, 14 and 27: auth writes that fail must not report OK.

``DatabaseManager._mutate`` returns False on a write failure instead of
raising, and three auth routes discarded that boolean:

  * login (14)        — 200 with a token whose session row was never written,
                        so every later request 401s (endless login loop).
  * set_password (3)  — 200 while the old password stays live, and/or while
                        every 30-day session survives a password change the UI
                        promised would revoke them.
  * logout (27)       — 200 "signed out" while the session row, and the token,
                        remain valid for the rest of the TTL.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a-whole-new-secret"


def _clear_auth():
    try:
        dm = DatabaseManager()
        dm.clear_password()
        dm.delete_all_sessions()
        dm.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_auth():
    from backend.api.routes import auth as auth_routes
    previous_nonce = registry.auth_nonce
    registry.auth_nonce = ""
    _clear_auth()
    auth_routes._login_fails.clear()
    yield
    _clear_auth()
    auth_routes._login_fails.clear()
    registry.auth_nonce = previous_nonce


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _set_first_password(client, password=PASSWORD):
    resp = client.post("/auth/set-password", json={"new_password": password})
    assert resp.status_code == 200, resp.text


def _login(client, password=PASSWORD):
    resp = client.post("/auth/login", json={"password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ── positive controls: every healthy path still returns 200 ───────────

def test_login_still_issues_a_working_token(client):
    _set_first_password(client)
    token = _login(client)
    assert client.get("/results", headers=_auth(token)).status_code != 401


def test_password_change_still_succeeds_and_revokes(client):
    _set_first_password(client)
    token = _login(client)
    resp = client.post("/auth/set-password", headers=_auth(token),
                       json={"new_password": NEW_PASSWORD,
                             "current_password": PASSWORD})
    assert resp.status_code == 200, resp.text
    assert client.get("/results", headers=_auth(token)).status_code == 401
    assert client.post("/auth/login",
                       json={"password": NEW_PASSWORD}).status_code == 200


def test_logout_still_succeeds_and_invalidates(client):
    _set_first_password(client)
    token = _login(client)
    assert client.post("/auth/logout", headers=_auth(token)).status_code == 200
    assert client.get("/results", headers=_auth(token)).status_code == 401


def test_logout_succeeds_for_the_nonce_with_no_session_row(client):
    # DISAGREEING CASE. The obvious wrong fix is to require the DELETE to have
    # removed a row (rowcount > 0). The desktop nonce has no auth_sessions row
    # at all, and its logout is documented as a no-op — that implementation
    # would 500 here, this one must stay 200.
    registry.auth_nonce = "test-nonce"
    assert client.post("/auth/logout",
                       headers=_auth("test-nonce")).status_code == 200


# ── login: unpersisted session (finding 14) ───────────────────────────

def test_login_reports_500_when_the_session_write_fails(client, monkeypatch):
    _set_first_password(client)
    monkeypatch.setattr(registry.db, "create_session", lambda *a, **k: False)
    resp = client.post("/auth/login", json={"password": PASSWORD})
    assert resp.status_code == 500, resp.text
    # No token may be handed out — one that 401s everywhere is worse than none.
    assert "token" not in resp.json()


# ── set-password: unpersisted hash / surviving sessions (finding 3) ───

def test_set_password_failure_is_atomic_and_reported(client, monkeypatch):
    _set_first_password(client)
    token = _login(client)
    calls = []
    monkeypatch.setattr(registry.db, "set_password_hash",
                        lambda h: calls.append("hash") or False)
    monkeypatch.setattr(registry.db, "delete_all_sessions",
                        lambda: calls.append("sessions") or True)
    resp = client.post("/auth/set-password", headers=_auth(token),
                       json={"new_password": NEW_PASSWORD,
                             "current_password": PASSWORD})
    assert resp.status_code == 500, resp.text
    # Ordering is load-bearing: wiping sessions after a failed hash write is
    # Case A — signed out everywhere with only the OLD password working.
    assert calls == ["hash"]
    assert client.post("/auth/login",
                       json={"password": PASSWORD}).status_code == 200
    assert client.post("/auth/login",
                       json={"password": NEW_PASSWORD}).status_code == 401


def test_set_password_reports_500_when_sessions_survive(client, monkeypatch):
    _set_first_password(client)
    token = _login(client)
    monkeypatch.setattr(registry.db, "delete_all_sessions", lambda: False)
    resp = client.post("/auth/set-password", headers=_auth(token),
                       json={"new_password": NEW_PASSWORD,
                             "current_password": PASSWORD})
    assert resp.status_code == 500, resp.text
    assert "session" in resp.json()["detail"].lower()
    # DISAGREEING CASE. An implementation that checked delete_all_sessions
    # first, or bailed out without writing, would also 500 here — but the
    # caller is being told to use the NEW password, so it must really be live.
    assert client.post("/auth/login",
                       json={"password": NEW_PASSWORD}).status_code == 200


# ── logout: surviving session row (finding 27) ────────────────────────

def test_logout_reports_500_when_the_delete_fails(client, monkeypatch):
    _set_first_password(client)
    token = _login(client)
    monkeypatch.setattr(registry.db, "delete_session", lambda h: False)
    resp = client.post("/auth/logout", headers=_auth(token))
    assert resp.status_code == 500, resp.text
    # The point of the error: that token really is still a working credential.
    assert client.get("/results", headers=_auth(token)).status_code != 401
