"""Tests for the system API endpoints (health, shutdown)."""
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app, _should_auto_connect_plex
from backend.api.dependencies import registry


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client():
    """A client against an auth-enabled app (a token is required)."""
    previous = registry.auth_nonce
    registry.auth_nonce = "test-nonce"
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    try:
        with TestClient(app) as c:
            yield c
    finally:
        registry.auth_nonce = previous


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data


def test_health_includes_plex_status(client):
    resp = client.get("/health")
    data = resp.json()
    assert "plex_connected" in data


def test_shutdown_returns_accepted(client):
    resp = client.post("/shutdown")
    assert resp.status_code == 202
    assert resp.json()["status"] == "shutting_down"


@pytest.mark.parametrize(
    "origin",
    [
        "https://tauri.localhost",  # desktop Tauri (useHttpsScheme)
        "http://tauri.localhost",  # Tauri >=2.x default on Windows + Android
        "tauri://localhost",  # Linux/macOS custom protocol
    ],
)
def test_cors_allows_tauri_origins(client, origin):
    resp = client.get("/health", headers={"Origin": origin})
    assert resp.headers.get("access-control-allow-origin") == origin


def test_cors_rejects_unknown_origin(client):
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.parametrize(
    "origin",
    [
        "https://tauri.localhost",
        "http://tauri.localhost",
        "tauri://localhost",
    ],
)
def test_cors_preflight_allowed_when_auth_enabled(auth_client, origin):
    """Regression: a CORS preflight (OPTIONS, no credentials) to a *protected*
    route must still receive CORS headers when auth is enabled. Otherwise the
    auth middleware 401s the preflight, the browser never sends the real authed
    request, and a non-same-origin client (the Android APK) can't reach the API.
    """
    resp = auth_client.options(
        "/results",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == origin


def test_protected_route_requires_token_when_auth_enabled(auth_client):
    """The OPTIONS pass-through must not weaken auth for real methods."""
    resp = auth_client.get("/results")
    assert resp.status_code == 401


def test_protected_route_accepts_valid_token(auth_client):
    resp = auth_client.get("/results", headers={"Authorization": "Bearer test-nonce"})
    assert resp.status_code != 401


class TestShouldAutoConnectPlex:
    """Startup auto-connect gate must fire for both direct and account modes."""

    def test_direct_mode_with_url_and_token(self):
        assert _should_auto_connect_plex({
            "auto_connect_plex": True,
            "plex_connection_mode": "direct",
            "plex_url": "http://localhost:32400",
            "plex_token": "tok",
        })

    def test_direct_mode_is_the_default_when_mode_absent(self):
        assert _should_auto_connect_plex({
            "auto_connect_plex": True,
            "plex_url": "http://localhost:32400",
            "plex_token": "tok",
        })

    def test_direct_mode_missing_token_is_skipped(self):
        assert not _should_auto_connect_plex({
            "auto_connect_plex": True,
            "plex_connection_mode": "direct",
            "plex_url": "http://localhost:32400",
            "plex_token": "",
        })

    def test_account_mode_with_username_and_password(self):
        """Regression: account mode has no URL/token but must still auto-connect."""
        assert _should_auto_connect_plex({
            "auto_connect_plex": True,
            "plex_connection_mode": "account",
            "plex_username": "user@example.com",
            "plex_password": "pw",
            # no plex_url / plex_token — discovered after plex.tv sign-in
        })

    def test_account_mode_missing_password_is_skipped(self):
        assert not _should_auto_connect_plex({
            "auto_connect_plex": True,
            "plex_connection_mode": "account",
            "plex_username": "user@example.com",
            "plex_password": "",
        })

    def test_disabled_when_auto_connect_off(self):
        assert not _should_auto_connect_plex({
            "auto_connect_plex": False,
            "plex_connection_mode": "account",
            "plex_username": "user@example.com",
            "plex_password": "pw",
        })


def test_health_reports_arm_revision_lifecycle_states(client):
    """R21-4. `revision_lifecycle_summary()` existed but nothing called it, so
    a parser or request-definition change was still an operator-silent event.

    A diagnostic nobody consumes is a different shape of the same silence.
    """
    data = client.get("/health").json()
    assert "arm_revisions" in data, (
        "the lifecycle report has no consumer, so an arm whose active revision "
        "has no evidence stays invisible")


def test_health_arm_revisions_names_no_arms(client):
    """COUNTS only. This route is unauthenticated -- a watcher needs to know
    that some arm is in the state, not which feeds are configured."""
    states = client.get("/health").json().get("arm_revisions")
    if states is None:
        return  # sub-report unavailable in this fixture; absence is informative
    assert isinstance(states, dict)
    for key in states:
        assert not key.startswith("arm."), (
            "an arm id leaked onto the unauthenticated health route: %s" % key)
    assert set(states) <= {"observed", "active_revision_unobserved",
                           "no_evidence", "undeclared_arm"}, states


def test_health_survives_a_failing_lifecycle_report(client, monkeypatch):
    """Health must never fail because a sub-report failed -- an absent key is
    itself informative and the watcher treats it as unknown."""
    from backend.api.dependencies import registry as reg
    if reg.db is None:
        return
    monkeypatch.setattr(
        reg.db, "revision_lifecycle_summary",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
