"""The scoped DV ingest key authorizes ONLY POST /rename/dv-host-rows.

Peer review (2026-08-10) established that a normal session token is a 30-day,
whole-API credential that reaches the destructive /rename job routes. The DV
host detector must not hold that. This suite is the negative-scope proof: a valid
ingest key admits exactly the one method+path and NOTHING else, and the normal
session path is unchanged.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.dependencies import registry
from backend.database import DatabaseManager

PASSWORD = "correct horse battery"
SECRET = "dv-ingest-secret-256bit-example-value"
SECRET_HASH = hashlib.sha256(SECRET.encode("utf-8")).hexdigest()


def _clear():
    try:
        dm = DatabaseManager()
        dm.clear_password(); dm.delete_all_sessions(); dm.clear_dv_scans(); dm.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset():
    previous_nonce = registry.auth_nonce
    registry.auth_nonce = ""
    _clear()
    yield
    _clear()
    registry.auth_nonce = previous_nonce


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _set_password(client):
    # A set password forces auth on regardless of the ambient SCANHOUND_ALLOW_OPEN
    # (see test_api_auth.test_allow_open_only_matters_when_no_credential).
    assert client.post("/auth/set-password",
                       json={"new_password": PASSWORD}).status_code == 200


def _login(client):
    return client.post("/auth/login", json={"password": PASSWORD}).json()["token"]


def _key(secret=SECRET):
    return {"X-DV-Ingest-Key": secret}


def _valid_body():
    return {"schema_version": 1, "source_rows": 1,
            "rows": [{"path": "Y:/M/a.mkv", "dv_layer": "fel", "title": "A"}]}


# ── the key admits exactly the ingest endpoint ───────────────────────────────

def test_valid_key_admits_and_persists(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    registry.db = DatabaseManager(); registry.db.clear_dv_scans()
    r = client.post("/rename/dv-host-rows", headers=_key(), json=_valid_body())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["processed"] == 1
    assert registry.db.get_dv_scan("Y:/M/a.mkv")["source"] == "scan"


def test_normal_session_still_admits_dv_host_rows(client, monkeypatch):
    """The scoped key is additive: a real admin session still works here."""
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    token = _login(client)
    registry.db = DatabaseManager(); registry.db.clear_dv_scans()
    r = client.post("/rename/dv-host-rows",
                    headers={"Authorization": f"Bearer {token}"}, json=_valid_body())
    assert r.status_code == 200, r.text


# ── the key admits NOTHING else (the whole point) ────────────────────────────

def test_key_does_not_admit_destructive_rename_route(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    # The apply route moves files. The key must not reach it.
    assert client.post("/rename/jobs/1/apply", headers=_key(),
                       json={}).status_code == 401


def test_key_does_not_admit_other_rename_paths(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    assert client.get("/rename/dv-scans", headers=_key()).status_code == 401


def test_key_does_not_admit_other_segments(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    assert client.get("/results", headers=_key()).status_code == 401


def test_key_is_post_only_on_the_ingest_path(client, monkeypatch):
    """Method gate: the key authorizes POST, not a GET on the same path."""
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    assert client.get("/rename/dv-host-rows", headers=_key()).status_code == 401


# ── bad / missing / unconfigured key ─────────────────────────────────────────

def test_wrong_key_rejected(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    r = client.post("/rename/dv-host-rows",
                    headers={"X-DV-Ingest-Key": "not-the-secret"}, json=_valid_body())
    assert r.status_code == 401


def test_missing_key_rejected(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    assert client.post("/rename/dv-host-rows",
                       json=_valid_body()).status_code == 401


def test_key_ignored_when_not_configured(client, monkeypatch):
    """Feature off: with no configured hash, even the 'right' secret is nothing."""
    monkeypatch.delenv("SCANHOUND_DV_INGEST_KEY_SHA256", raising=False)
    _set_password(client)
    assert client.post("/rename/dv-host-rows", headers=_key(),
                       json=_valid_body()).status_code == 401


# ── path-canonicalization edges (the exact match IS the boundary) ─────────────

def test_key_rejected_on_trailing_slash(client, monkeypatch):
    # "…/dv-host-rows/" != the authorized path; auth runs before any router
    # trailing-slash redirect, so the key is refused (401), fail closed.
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    assert client.post("/rename/dv-host-rows/", headers=_key(),
                       json=_valid_body()).status_code == 401


def test_key_rejected_on_double_slash(client, monkeypatch):
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    assert client.post("/rename//dv-host-rows", headers=_key(),
                       json=_valid_body()).status_code == 401


def test_uppercase_path_does_not_reach_ingest_handler(client, monkeypatch):
    # Case-sensitive routing: "/RENAME/…" is not a protected segment and does not
    # match the (lowercase) ingest route, so the POST never reaches the DV
    # handler — the key grants nothing. The exact code is 404 (no route) or 405
    # (matched only the SPA GET catch-all); either proves no ingest happened.
    # Pinned so a later routing/proxy change can't silently move the boundary.
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH)
    _set_password(client)
    r = client.post("/RENAME/dv-host-rows", headers=_key(), json=_valid_body())
    assert r.status_code in (404, 405), r.status_code


# ── malformed configured hash fails closed (Finding 4) ───────────────────────

def test_malformed_configured_hash_disables_key(monkeypatch):
    from backend.api import dependencies as deps
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", "not-64-hex-chars")
    assert deps.dv_ingest_key_hash() == ""                      # disabled
    assert deps.dv_ingest_key_authorized(SECRET) is False       # nothing authorizes
    # A too-short (63-char) value is also malformed → disabled.
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH[:-1])
    assert deps.dv_ingest_key_hash() == ""
    # A valid 64-hex digest passes through, normalized to lowercase.
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", SECRET_HASH.upper())
    assert deps.dv_ingest_key_hash() == SECRET_HASH
    assert deps.dv_ingest_key_authorized(SECRET) is True


def test_malformed_hash_warns_once_without_value(monkeypatch, caplog):
    import logging
    from backend.api import dependencies as deps
    monkeypatch.setattr(deps, "_dv_ingest_bad_hash_warned", False)
    monkeypatch.setenv("SCANHOUND_DV_INGEST_KEY_SHA256", "SEKRET-typo-not-64-hex")
    with caplog.at_level(logging.WARNING, logger="backend.api.dependencies"):
        deps.dv_ingest_key_hash()
        deps.dv_ingest_key_hash()  # second call must NOT warn again
    warns = [r for r in caplog.records
             if "SCANHOUND_DV_INGEST_KEY_SHA256" in r.getMessage()]
    assert len(warns) == 1, "the malformed-hash warning must fire exactly once"
    assert "SEKRET-typo-not-64-hex" not in warns[0].getMessage(), \
        "the malformed value must never be logged"
