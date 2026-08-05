"""Audit pass 2, finding 17: a WebSocket credential that survives a proxy log.

The socket's only auth input was ``?token=<30-day session token>``, which
NPM/nginx, Cloudflare and uvicorn's own access log all record verbatim. This
covers the backend half of the fix: ``POST /auth/ws-ticket`` mints a
single-use, seconds-long ticket that the handshake accepts in place of the
token. ``?token=`` still works — the frontend (frontend/src/lib/stores/
connection.ts) has to switch over before the leak is actually closed.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api.main import create_app
from backend.api import dependencies as deps
from backend.api import ws as ws_module
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
    previous_nonce = registry.auth_nonce
    registry.auth_nonce = ""
    _clear_auth()
    deps._ws_tickets.clear()
    yield
    _clear_auth()
    deps._ws_tickets.clear()
    registry.auth_nonce = previous_nonce


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


def _ticket(client, token):
    resp = client.post("/auth/ws-ticket", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["ticket"]


# ── positive controls ─────────────────────────────────────────────────

def test_ticket_authorizes_the_socket(client):
    token = _logged_in(client)
    with client.websocket_connect(f"/ws?ticket={_ticket(client, token)}") as ws:
        assert ws.receive_json()["type"] == "connected"


def test_token_query_param_still_works(client):
    # The frontend still sends ?token=; accepting the ticket must not break it.
    token = _logged_in(client)
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "connected"


def test_ticket_minted_from_the_desktop_nonce_works(client):
    registry.auth_nonce = "secret-nonce"
    with client.websocket_connect(
            f"/ws?ticket={_ticket(client, 'secret-nonce')}") as ws:
        assert ws.receive_json()["type"] == "connected"


# ── the ticket is worth less than the token it replaces ───────────────

def test_ws_ticket_route_requires_a_credential(client):
    client.post("/auth/set-password", json={"new_password": PASSWORD})
    assert client.post("/auth/ws-ticket").status_code == 401


def test_ticket_is_single_use(client):
    token = _logged_in(client)
    ticket = _ticket(client, token)
    with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
        assert ws.receive_json()["type"] == "connected"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
            ws.receive_json()


def test_expired_ticket_is_rejected(client, monkeypatch):
    token = _logged_in(client)
    monkeypatch.setattr(deps, "_WS_TICKET_TTL_S", -1.0)
    ticket = _ticket(client, token)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
            ws.receive_json()


def test_unknown_ticket_is_rejected(client):
    _logged_in(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?ticket=not-a-real-ticket") as ws:
            ws.receive_json()


def test_ticket_is_not_an_http_credential(client):
    # DISAGREEING CASE. A ticket store wired into token_authorized (or a route
    # that just handed back the session token under a new name) would pass
    # every socket test above while widening HTTP auth. A ticket must open
    # exactly one socket and nothing else.
    token = _logged_in(client)
    ticket = _ticket(client, token)
    assert client.get("/results", headers=_auth(ticket)).status_code == 401
    assert ticket != token


def test_ticket_socket_still_follows_session_revocation(client, monkeypatch):
    # The ticket is consumed at handshake, so the socket has to keep
    # re-checking the SESSION it was minted from (finding 16), not the ticket.
    monkeypatch.setattr(ws_module, "_REVALIDATE_INTERVAL_S", 0.05)
    token = _logged_in(client)
    ticket = _ticket(client, token)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws?ticket={ticket}") as ws:
            assert ws.receive_json()["type"] == "connected"
            assert client.post("/auth/logout",
                               headers=_auth(token)).status_code == 200
            ws.receive_json()
