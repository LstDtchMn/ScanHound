import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager
from backend.rename.dv_import import import_dv_host_db


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


def test_import_creates_scan_rows(tmp_path):
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [
        ("Y:/M/a.mkv", "fel", 111.0, 1000, "A"),
        ("Y:/M/b.mkv", "mel", 222.0, 2000, "B"),
    ])
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    res = import_dv_host_db(dm, str(host))
    assert res == {"imported": 2, "updated": 0}
    row = dm.get_dv_scan("Y:/M/a.mkv")
    assert row["dv_layer"] == "fel" and row["source"] == "scan"
    assert row["sig_mtime"] == 111.0 and row["sig_size"] == 1000
    dm.close()


def test_reimport_is_idempotent_update(tmp_path):
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [("Y:/M/a.mkv", "fel", 111.0, 1000, "A")])
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    import_dv_host_db(dm, str(host))
    res2 = import_dv_host_db(dm, str(host))
    assert res2 == {"imported": 0, "updated": 1}
    assert dm.count_dv_scans_by_layer(source="scan") == {"fel": 1}
    dm.close()


def test_import_overwrites_seed_row(tmp_path):
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [("Y:/M/a.mkv", "fel", 111.0, 1000, "A")])
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    dm.upsert_dv_scan("Y:/M/a.mkv", "unknown", title="A", source="seed")
    import_dv_host_db(dm, str(host))
    row = dm.get_dv_scan("Y:/M/a.mkv")
    assert row["source"] == "scan" and row["dv_layer"] == "fel"
    dm.close()


def test_missing_host_db_returns_zero(tmp_path):
    dm = DatabaseManager(db_path=str(tmp_path / "c.db"))
    res = import_dv_host_db(dm, str(tmp_path / "nope.db"))
    assert res == {"imported": 0, "updated": 0}
    dm.close()


def test_dv_import_endpoint(client, tmp_path):
    from backend.api.dependencies import registry
    from backend.database import DatabaseManager
    host = tmp_path / "dv_host.db"
    _make_host_db(host, [("Y:/M/a.mkv", "fel", 1.0, 10, "A")])
    dm = DatabaseManager(); dm.clear_dv_scans()
    registry.db = dm
    r = client.post("/rename/dv-import", json={"host_db_path": str(host)})
    assert r.status_code == 200
    assert r.json() == {"imported": 1, "updated": 0}
    assert dm.get_dv_scan("Y:/M/a.mkv")["source"] == "scan"
    dm.clear_dv_scans()
