import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager
from backend.rename.dv_import import (
    DvHostReadError, import_dv_host_db, import_dv_rows)


@pytest.fixture(autouse=True)
def _reset_jobs():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_rename_jobs(); dm.clear_dv_scans(); dm.close()
        except Exception:
            pass
    _clear(); yield; _clear()


@pytest.fixture
def client():
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    with TestClient(app) as c:
        yield c


def _make_host_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE dv_host (path TEXT PRIMARY KEY, dv_layer TEXT, "
        "sig_mtime REAL, sig_size INTEGER, title TEXT, scanned_at TIMESTAMP)")
    conn.executemany(
        "INSERT INTO dv_host (path, dv_layer, sig_mtime, sig_size, title) "
        "VALUES (?,?,?,?,?)", rows)
    conn.commit(); conn.close()


# ── import_dv_rows: the shared upsert (row-POST path) ────────────────────────

def test_import_dv_rows_counts(tmp_path):
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    try:
        res = import_dv_rows(dm, [
            {"path": "Y:/a.mkv", "dv_layer": "fel", "title": "A",
             "sig_mtime": 1.0, "sig_size": 10},
            {"path": "Y:/b.mkv", "dv_layer": "mel"},
            {"path": "", "dv_layer": "fel"},        # no path -> skipped
        ])
        assert res == {"source_rows": 3, "processed": 2, "imported": 2,
                       "updated": 0, "failed": 0}
        row = dm.get_dv_scan("Y:/a.mkv")
        assert row["dv_layer"] == "fel" and row["source"] == "scan"
        assert row["sig_mtime"] == 1.0 and row["sig_size"] == 10
    finally:
        dm.close()


def test_import_dv_rows_counts_failures(tmp_path, monkeypatch):
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    try:
        monkeypatch.setattr(dm, "upsert_dv_scan", lambda *a, **k: False)
        res = import_dv_rows(dm, [{"path": "Y:/a.mkv", "dv_layer": "fel"}])
        assert res["failed"] == 1 and res["processed"] == 0
    finally:
        dm.close()


# ── import_dv_host_db: the legacy file path, now failing loudly ──────────────

def test_import_creates_scan_rows(tmp_path):
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [
        ("Y:/M/a.mkv", "fel", 111.0, 1000, "A"),
        ("Y:/M/b.mkv", "mel", 222.0, 2000, "B"),
    ])
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    try:
        res = import_dv_host_db(dm, str(host))
        assert res["imported"] == 2 and res["updated"] == 0 and res["failed"] == 0
        assert res["source_rows"] == 2 and res["processed"] == 2
        row = dm.get_dv_scan("Y:/M/a.mkv")
        assert row["dv_layer"] == "fel" and row["source"] == "scan"
    finally:
        dm.close()


def test_reimport_is_idempotent_update(tmp_path):
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [("Y:/M/a.mkv", "fel", 111.0, 1000, "A")])
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    try:
        import_dv_host_db(dm, str(host))
        res2 = import_dv_host_db(dm, str(host))
        assert res2["imported"] == 0 and res2["updated"] == 1
        assert dm.count_dv_scans_by_layer(source="scan") == {"fel": 1}
    finally:
        dm.close()


def test_import_overwrites_seed_row(tmp_path):
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [("Y:/M/a.mkv", "fel", 111.0, 1000, "A")])
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    try:
        dm.upsert_dv_scan("Y:/M/a.mkv", "unknown", title="A", source="seed")
        import_dv_host_db(dm, str(host))
        row = dm.get_dv_scan("Y:/M/a.mkv")
        assert row["source"] == "scan" and row["dv_layer"] == "fel"
    finally:
        dm.close()


def test_missing_host_db_raises(tmp_path):
    """Round-4 finding 2: an unreadable host DB is NOT a zero-row success."""
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    try:
        with pytest.raises(DvHostReadError):
            import_dv_host_db(dm, str(tmp_path / "nope.db"))
    finally:
        dm.close()


# ── the endpoints ────────────────────────────────────────────────────────────

def test_dv_import_endpoint(client, tmp_path, monkeypatch):
    from backend.api.dependencies import registry
    from backend.api.routes import rename as rename_routes
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [("Y:/M/a.mkv", "fel", 1.0, 10, "A")])
    monkeypatch.setattr(
        rename_routes, "_DEFAULT_DV_HOST_DB", str(tmp_path / "dv_host.db"))
    dm = DatabaseManager(); dm.clear_dv_scans()
    registry.db = dm
    r = client.post("/rename/dv-import", json={"host_db_path": str(host)})
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 1 and body["updated"] == 0 and body["failed"] == 0
    assert dm.get_dv_scan("Y:/M/a.mkv")["source"] == "scan"
    dm.clear_dv_scans()


def test_dv_import_endpoint_missing_db_is_503(client, tmp_path, monkeypatch):
    from backend.api.dependencies import registry
    from backend.api.routes import rename as rename_routes
    monkeypatch.setattr(
        rename_routes, "_DEFAULT_DV_HOST_DB", str(tmp_path / "dv_host.db"))
    dm = DatabaseManager(); registry.db = dm
    # No file at that path -> a read failure must NOT be a zero-row 200.
    r = client.post("/rename/dv-import", json={})
    assert r.status_code == 503


def test_dv_host_rows_endpoint_success(client):
    from backend.api.dependencies import registry
    dm = DatabaseManager(); dm.clear_dv_scans()
    registry.db = dm
    r = client.post("/rename/dv-host-rows", json={
        "rows": [{"path": "Y:/M/a.mkv", "dv_layer": "fel", "title": "A"},
                 {"path": "Y:/M/b.mkv", "dv_layer": "mel"}],
        "source_rows": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["imported"] == 2 and body["processed"] == 2 and body["failed"] == 0
    assert dm.get_dv_scan("Y:/M/a.mkv")["source"] == "scan"
    dm.clear_dv_scans()


def test_dv_host_rows_source_rows_mismatch_is_422(client):
    from backend.api.dependencies import registry
    registry.db = DatabaseManager()
    r = client.post("/rename/dv-host-rows", json={
        "rows": [{"path": "Y:/M/a.mkv", "dv_layer": "fel"}], "source_rows": 5})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "source_rows_mismatch"


def test_dv_host_rows_partial_failure_is_500(client, monkeypatch):
    from backend.api.dependencies import registry
    dm = DatabaseManager(); dm.clear_dv_scans()
    monkeypatch.setattr(dm, "upsert_dv_scan", lambda *a, **k: False)
    registry.db = dm
    r = client.post("/rename/dv-host-rows", json={
        "rows": [{"path": "Y:/M/a.mkv", "dv_layer": "fel"}], "source_rows": 1})
    assert r.status_code == 500
    assert r.json()["detail"]["ok"] is False
    assert r.json()["detail"]["failed"] == 1
    dm.clear_dv_scans()
