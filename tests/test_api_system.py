"""Tests for the system API endpoints (health, shutdown)."""
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


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
