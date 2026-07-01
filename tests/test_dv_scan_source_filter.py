"""dv_scan source filter: DV panel counts/list must exclude dead seed rows
by default at the API layer, while the DB helpers stay backward-compatible
(no source filter = all rows) for existing callers."""
import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.database import DatabaseManager


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


def test_count_and_list_exclude_seed_by_default(tmp_path):
    dm = DatabaseManager(db_path=str(tmp_path / "t.db"))
    # seed rows (dead bootstrap) + real scan rows
    dm.upsert_dv_scan("/media/seed_a.mkv", "fel", title="Seed A", source="seed")
    dm.upsert_dv_scan("/media/seed_b.mkv", "mel", title="Seed B", source="seed")
    dm.upsert_dv_scan("/media/scan_a.mkv", "fel", title="Scan A", source="scan")
    dm.upsert_dv_scan("/media/scan_b.mkv", "fel", title="Scan B", source="scan")
    dm.upsert_dv_scan("/media/scan_c.mkv", "mel", title="Scan C", source="scan")

    # default (no filter) = every row, backward-compatible
    assert dm.count_dv_scans_by_layer() == {"fel": 3, "mel": 2}

    # scan-only = the real detected counts
    assert dm.count_dv_scans_by_layer(source="scan") == {"fel": 2, "mel": 1}

    scan_rows = dm.get_dv_scans(source="scan")
    assert {r["path"] for r in scan_rows} == {
        "/media/scan_a.mkv", "/media/scan_b.mkv", "/media/scan_c.mkv"}

    # layer + source compose
    fel_scan = dm.get_dv_scans(dv_layer="fel", source="scan")
    assert {r["path"] for r in fel_scan} == {"/media/scan_a.mkv", "/media/scan_b.mkv"}

    dm.close()


def test_dv_scans_endpoint_scan_source_only(client):
    dm = DatabaseManager()
    dm.clear_dv_scans()
    dm.upsert_dv_scan("/m/seed.mkv", "fel", source="seed")
    dm.upsert_dv_scan("/m/scan.mkv", "fel", source="scan")
    r = client.get("/rename/dv-scans")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"fel": 1}
    assert [s["path"] for s in body["scans"]] == ["/m/scan.mkv"]
    dm.clear_dv_scans()
