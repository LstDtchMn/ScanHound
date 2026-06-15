"""Tests for the WebSocket hub."""
import pytest
from fastapi.testclient import TestClient
from backend.api.main import create_app


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def test_ws_connect_and_receive_welcome(client):
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        assert data["type"] == "connected"
        assert "version" in data["data"]


def test_ws_invalid_json(client):
    with client.websocket_connect("/ws") as ws:
        _ = ws.receive_json()  # welcome
        ws.send_text("not valid json")
        data = ws.receive_json()
        assert data["type"] == "error"
