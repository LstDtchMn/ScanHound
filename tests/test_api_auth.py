"""Tests for the password-auth endpoints and bearer-token middleware.

Covers the new settable-password flow (set / login / change / logout), that the
desktop nonce path still works, and that the middleware guards API routes while
leaving the SPA shell and the login endpoints open.
"""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager
from backend import auth_service

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
    """Clear credentials + sessions and the nonce between tests (shared DB)."""
    from backend.api.routes import auth as auth_routes
    previous_nonce = registry.auth_nonce
    registry.auth_nonce = ""
    _clear_auth()
    auth_routes._login_fails.clear()  # reset the login rate-limiter
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
    """Set the initial password (open mode — no token needed)."""
    resp = client.post("/auth/set-password", json={"new_password": password})
    assert resp.status_code == 200, resp.text
    return resp


def _login(client, password=PASSWORD):
    resp = client.post("/auth/login", json={"password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# ── status ────────────────────────────────────────────────────────────

def test_status_open_when_nothing_configured(client):
    data = client.get("/auth/status").json()
    assert data == {"auth_required": False, "has_password": False, "nonce_active": False}


def test_status_locked_after_password_set(client):
    _set_first_password(client)
    data = client.get("/auth/status").json()
    assert data["has_password"] is True
    assert data["auth_required"] is True


def test_status_reachable_without_token_when_locked(client):
    _set_first_password(client)
    # /auth/status is exempt — the login page needs it before holding a token.
    assert client.get("/auth/status").status_code == 200


# ── middleware gating ─────────────────────────────────────────────────

def test_api_open_before_password(client):
    # No nonce, no password → dev/open mode, API reachable without a token.
    assert client.get("/results").status_code != 401


def test_api_locked_after_password(client):
    _set_first_password(client)
    assert client.get("/results").status_code == 401


def test_spa_and_static_paths_never_gated(client):
    _set_first_password(client)
    # Non-API paths (SPA shell / client routes) must not 401, or the login
    # page could never load. Without a frontend build these 404, never 401.
    for path in ("/login", "/", "/_app/immutable/whatever.js"):
        assert client.get(path).status_code != 401, path


# ── login ─────────────────────────────────────────────────────────────

def test_login_requires_configured_password(client):
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 400


def test_login_wrong_password_rejected(client):
    _set_first_password(client)
    assert client.post("/auth/login", json={"password": "nope"}).status_code == 401


def test_login_issues_usable_token(client):
    _set_first_password(client)
    token = _login(client)
    assert client.get("/results", headers=_auth(token)).status_code != 401
    assert client.get("/results", headers=_auth("garbage")).status_code == 401


def test_login_rate_limited_after_repeated_failures(client):
    from backend.api.routes import auth as auth_routes
    _set_first_password(client)
    # Exhaust the failed-attempt budget for this client.
    for _ in range(auth_routes._RATE_MAX_FAILS):
        assert client.post("/auth/login", json={"password": "nope"}).status_code == 401
    # Further attempts — even with the *correct* password — are now throttled.
    assert client.post("/auth/login", json={"password": "nope"}).status_code == 429
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 429


def test_login_success_clears_failure_counter(client):
    from backend.api.routes import auth as auth_routes
    _set_first_password(client)
    # A few failures, then a success, must reset the budget so the next typo
    # doesn't immediately lock the user out.
    for _ in range(3):
        client.post("/auth/login", json={"password": "nope"})
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 200
    # Success wiped the counter — no IP retains any recorded failures.
    assert all(len(d) == 0 for d in auth_routes._login_fails.values())


# ── set / change password ─────────────────────────────────────────────

def test_set_password_enforces_min_length(client):
    assert client.post("/auth/set-password", json={"new_password": "short"}).status_code == 400


def test_change_password_requires_token_and_current(client):
    _set_first_password(client)
    token = _login(client)
    # Once a password exists the route is guarded: no token → 401 (middleware).
    assert client.post("/auth/set-password",
                       json={"new_password": NEW_PASSWORD}).status_code == 401
    # Token but wrong current password → 401 (route check).
    assert client.post("/auth/set-password", headers=_auth(token),
                       json={"new_password": NEW_PASSWORD,
                             "current_password": "wrong"}).status_code == 401
    # Token + correct current → success.
    assert client.post("/auth/set-password", headers=_auth(token),
                       json={"new_password": NEW_PASSWORD,
                             "current_password": PASSWORD}).status_code == 200
    # New password works; old one no longer does.
    assert client.post("/auth/login", json={"password": NEW_PASSWORD}).status_code == 200
    assert client.post("/auth/login", json={"password": PASSWORD}).status_code == 401


def test_change_password_revokes_existing_sessions(client):
    _set_first_password(client)
    token = _login(client)
    assert client.get("/results", headers=_auth(token)).status_code != 401
    client.post("/auth/set-password", headers=_auth(token),
                json={"new_password": NEW_PASSWORD, "current_password": PASSWORD})
    # The old session token is wiped on a password change.
    assert client.get("/results", headers=_auth(token)).status_code == 401


# ── logout ────────────────────────────────────────────────────────────

def test_logout_invalidates_token(client):
    _set_first_password(client)
    token = _login(client)
    assert client.get("/results", headers=_auth(token)).status_code != 401
    assert client.post("/auth/logout", headers=_auth(token)).status_code == 200
    assert client.get("/results", headers=_auth(token)).status_code == 401


# ── nonce (desktop sidecar) ───────────────────────────────────────────

def test_nonce_still_authorizes(client):
    registry.auth_nonce = "test-nonce"
    assert client.get("/results").status_code == 401
    assert client.get("/results", headers=_auth("test-nonce")).status_code != 401


def test_nonce_active_reflected_in_status(client):
    registry.auth_nonce = "test-nonce"
    data = client.get("/auth/status").json()
    assert data["nonce_active"] is True
    assert data["auth_required"] is True
