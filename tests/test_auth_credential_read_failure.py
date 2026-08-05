"""Audit pass 2, finding 2: a failed credential read must not read as "no password".

``DatabaseManager._query`` returns its ``default`` on ANY exception, so
``has_password()`` answered False for an unreadable DB exactly as it does for a
fresh install. That False un-gated ``/auth/set-password`` at the middleware
(bootstrap exemption) AND skipped the current-password check inside the route,
so a disk I/O error / locked DB / partial migration re-opened unauthenticated
admin takeover.

The fix adds a third state ("unknown") in backend/api/dependencies.py and fails
closed on it at both consumers.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry, auth_enabled, credential_state
from backend.database import DatabaseManager

PASSWORD = "correct horse battery"
ATTACKER_PASSWORD = "attacker-chosen-pw"


def _clear_auth():
    try:
        dm = DatabaseManager()  # __init__ runs init_db(), restoring dropped tables
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


def _drop_credential_table(db):
    """Reproduce the real trigger: the SELECT raises "no such table", which
    ``_query`` swallows into its default (partial migration / rebuilt DB)."""
    conn = db.get_connection()
    conn.execute("DROP TABLE IF EXISTS auth_credentials")
    conn.commit()


class _CredentialReadSwitch:
    """Toggleable stand-in for ``_query`` returning the default on the
    credential SELECT — what the real method does when the read raises. Used
    where the test has to keep WRITES working so it can verify afterwards that
    the stored hash was never touched."""

    def __init__(self, db):
        self._real = db._query
        self.failing = False

    def __call__(self, sql, params=(), *, one=False, default=None):
        if self.failing and "auth_credentials" in sql:
            return default
        return self._real(sql, params, one=one, default=default)


# ── positive controls: the healthy paths still work ───────────────────

def test_credential_state_present_on_healthy_db(client):
    _set_first_password(client)
    assert credential_state(registry.db) == "present"
    assert auth_enabled() is True


def test_credential_state_absent_on_fresh_install(client):
    assert credential_state(registry.db) == "absent"
    assert auth_enabled() is False


def test_bootstrap_and_login_still_work_on_fresh_install(client, monkeypatch):
    # DISAGREEING CASE. An implementation that answered "unknown" whenever the
    # row is missing — or simply failed closed always — would pass every
    # takeover assertion below while bricking a real fresh install. Only a
    # correct three-state read passes both this and the takeover tests.
    monkeypatch.delenv("SCANHOUND_ALLOW_OPEN", raising=False)
    resp = client.post("/auth/set-password", json={"new_password": PASSWORD})
    assert resp.status_code == 200, resp.text
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 200


def test_password_change_still_works_with_correct_current(client):
    _set_first_password(client)
    token = client.post("/auth/login", json={"password": PASSWORD}).json()["token"]
    resp = client.post("/auth/set-password", headers=_auth(token),
                       json={"new_password": "a-whole-new-secret",
                             "current_password": PASSWORD})
    assert resp.status_code == 200, resp.text


# ── the defect ────────────────────────────────────────────────────────

def test_credential_state_unknown_when_read_fails(client):
    _set_first_password(client)
    _drop_credential_table(registry.db)
    # The conflation itself: has_password() cannot tell the two apart.
    assert registry.db.has_password() is False
    assert credential_state(registry.db) == "unknown"


def test_auth_stays_enabled_when_credential_unreadable(client):
    _set_first_password(client)
    _drop_credential_table(registry.db)
    assert auth_enabled() is True


def test_unauthenticated_takeover_blocked_when_credential_unreadable(client, monkeypatch):
    # End-to-end repro of the finding: no Authorization header, no
    # current_password, production posture (escape hatch unset). Before the
    # fix this returned 200 and replaced the stored hash.
    monkeypatch.delenv("SCANHOUND_ALLOW_OPEN", raising=False)
    _set_first_password(client)
    _drop_credential_table(registry.db)
    resp = client.post("/auth/set-password",
                       json={"new_password": ATTACKER_PASSWORD})
    assert resp.status_code == 401, resp.text


def test_route_refuses_when_credential_unreadable_even_with_a_token(client, monkeypatch):
    # Second layer: a caller the middleware DOES accept (the desktop nonce, or
    # any live session) must still not slip past the current-password check
    # just because the stored hash could not be read.
    _set_first_password(client)
    switch = _CredentialReadSwitch(registry.db)
    monkeypatch.setattr(registry.db, "_query", switch)
    registry.auth_nonce = "test-nonce"
    switch.failing = True
    resp = client.post("/auth/set-password", headers=_auth("test-nonce"),
                       json={"new_password": ATTACKER_PASSWORD})
    assert resp.status_code == 503, resp.text
    # Writes were never blocked in this test, so the stored hash proves the
    # route refused rather than merely reporting an error after writing.
    switch.failing = False
    assert client.post("/auth/login",
                       json={"password": PASSWORD}).status_code == 200
    assert client.post("/auth/login",
                       json={"password": ATTACKER_PASSWORD}).status_code == 401
