"""Tests for the WebSocket hub."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager

PASSWORD = "correct horse battery"


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
    previous_nonce = registry.auth_nonce
    registry.auth_nonce = ""
    _clear_auth()
    yield
    _clear_auth()
    registry.auth_nonce = previous_nonce


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


# ── auth handshake (regression: socket must honour the same gate as HTTP) ──

def test_ws_rejects_no_token_when_password_set(client):
    """A password (browser-login mode, empty nonce) must still gate the socket.

    Regression for the bypass where /ws only checked the nonce, so in
    password mode — where the nonce is empty — any/no token was accepted.
    """
    client.post("/auth/set-password", json={"new_password": PASSWORD})
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_ws_rejects_bad_token_when_password_set(client):
    client.post("/auth/set-password", json={"new_password": PASSWORD})
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=not-a-real-token") as ws:
            ws.receive_json()


def test_ws_accepts_valid_session_token(client):
    client.post("/auth/set-password", json={"new_password": PASSWORD})
    token = client.post("/auth/login", json={"password": PASSWORD}).json()["token"]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "connected"


def test_ws_accepts_desktop_nonce(client):
    registry.auth_nonce = "secret-nonce"
    with client.websocket_connect("/ws?token=secret-nonce") as ws:
        assert ws.receive_json()["type"] == "connected"


def test_ws_rejects_wrong_nonce(client):
    registry.auth_nonce = "secret-nonce"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()


def test_ws_open_when_allow_open_escape_hatch_set(client, monkeypatch):
    """No password, no nonce, SCANHOUND_ALLOW_OPEN=1 → dev/open mode keeps
    working without a token.

    Renamed from test_ws_open_when_auth_disabled: this used to pass on the
    ambient conftest default alone (the old guard ignored allow_open()
    entirely). Now explicit, mirroring tests/test_api_auth.py's pattern, so
    it actually exercises the escape hatch rather than an accident of the
    no-credential state.
    """
    monkeypatch.setenv("SCANHOUND_ALLOW_OPEN", "1")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"


def test_ws_rejects_no_token_when_no_credential_by_default(client, monkeypatch):
    """Fail-closed regression (SH-H01): with an empty-credential DB and the
    escape hatch unset, a no-token socket must be REJECTED — mirroring the
    HTTP middleware's fail-closed default. Previously the guard only checked
    auth_enabled() and ignored allow_open(), so this connected regardless.
    """
    monkeypatch.delenv("SCANHOUND_ALLOW_OPEN", raising=False)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
