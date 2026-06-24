"""Tests for the system API endpoints (health, shutdown)."""
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app
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
