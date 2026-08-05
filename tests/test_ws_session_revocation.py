"""Audit pass 2, finding 16: an established socket must honour revocation.

The WebSocket was authorized once at handshake and never again, so logout, a
password change and the 30-day expiry all left the socket streaming every
broadcast while the HTTP side correctly 401'd. The endpoint now re-checks the
same gate on every idle interval and before handling each frame.
"""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.main import create_app
from backend.api import ws as ws_module
from backend.api.dependencies import registry
from backend.database import DatabaseManager
from backend import auth_service

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a-whole-new-secret"

# Several revalidation intervals, so a socket that survives has genuinely been
# re-checked repeatedly rather than just not yet reached its first check.
_SETTLE_S = 0.5


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
    previous_nonce = registry.auth_nonce
    registry.auth_nonce = ""
    _clear_auth()
    yield
    _clear_auth()
    registry.auth_nonce = previous_nonce


@pytest.fixture(autouse=True)
def _fast_revalidation(monkeypatch):
    """60s in production; shortened here so an idle re-check happens promptly."""
    monkeypatch.setattr(ws_module, "_REVALIDATE_INTERVAL_S", 0.05)


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _logged_in(client):
    client.post("/auth/set-password", json={"new_password": PASSWORD})
    return client.post("/auth/login", json={"password": PASSWORD}).json()["token"]


# ── positive controls ─────────────────────────────────────────────────

def test_valid_socket_survives_many_revalidations(client):
    # DISAGREEING CASE. An implementation that closed the socket on every idle
    # timeout — or one that dropped the frame it was waiting on when the timer
    # fired — would pass all the revocation tests below. This one requires the
    # socket to live through ~10 re-checks and still round-trip a frame.
    token = _logged_in(client)
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "connected"
        time.sleep(_SETTLE_S)
        ws.send_text("not valid json")
        assert ws.receive_json()["type"] == "error"


def test_open_mode_socket_survives_revalidation(client, monkeypatch):
    # The escape hatch has no token to re-check; the periodic check must not
    # start closing sockets that the handshake rule accepts.
    monkeypatch.setenv("SCANHOUND_ALLOW_OPEN", "1")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        time.sleep(_SETTLE_S)
        ws.send_text("not valid json")
        assert ws.receive_json()["type"] == "error"


def test_nonce_socket_survives_revalidation(client):
    registry.auth_nonce = "secret-nonce"
    with client.websocket_connect("/ws?token=secret-nonce") as ws:
        assert ws.receive_json()["type"] == "connected"
        time.sleep(_SETTLE_S)
        ws.send_text("not valid json")
        assert ws.receive_json()["type"] == "error"


# ── revocation now reaches the socket ─────────────────────────────────

def test_socket_closes_after_logout(client):
    token = _logged_in(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "connected"
            assert client.post("/auth/logout",
                               headers=_auth(token)).status_code == 200
            ws.receive_json()  # blocks until the idle re-check closes it


def test_socket_closes_after_password_change(client):
    token = _logged_in(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "connected"
            resp = client.post("/auth/set-password", headers=_auth(token),
                               json={"new_password": NEW_PASSWORD,
                                     "current_password": PASSWORD})
            assert resp.status_code == 200, resp.text
            ws.receive_json()


def test_socket_closes_once_the_session_expires(client):
    token = _logged_in(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "connected"
            # Same row, expiry moved into the past (ON CONFLICT updates it) —
            # expiry is not revocation, and the socket outlived it too.
            registry.db.create_session(auth_service.hash_token(token),
                                       auth_service.session_expiry(ttl_days=-1))
            ws.receive_json()


def test_revoked_socket_closes_on_a_sent_frame_too(client, monkeypatch):
    # With the interval back at production length the idle path can't fire, so
    # this exercises the check that runs before handling a received frame.
    monkeypatch.setattr(ws_module, "_REVALIDATE_INTERVAL_S", 60.0)
    token = _logged_in(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?token={token}") as ws:
            assert ws.receive_json()["type"] == "connected"
            assert client.post("/auth/logout",
                               headers=_auth(token)).status_code == 200
            ws.send_text("not valid json")
            ws.receive_json()
