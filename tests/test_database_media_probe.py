import json
from backend.database import DatabaseManager


def _db(tmp_path):
    return DatabaseManager(db_path=str(tmp_path / "t.db"))


def test_upsert_and_get_media_probe(tmp_path):
    db = _db(tmp_path)
    payload = {"present": True, "resolution": "2160p", "size_bytes": 40000000000}
    assert db.upsert_media_probe("/m.mkv", json.dumps(payload), sig_mtime=100.0, sig_size=40000000000)
    row = db.get_media_probe("/m.mkv")
    assert row is not None
    assert json.loads(row["probe_json"]) == payload
    assert row["sig_mtime"] == 100.0
    assert row["sig_size"] == 40000000000


def test_get_media_probe_missing_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_media_probe("/no/such.mkv") is None


def test_media_probe_is_current_matches_within_1s_mtime_tolerance(tmp_path):
    db = _db(tmp_path)
    db.upsert_media_probe("/m.mkv", "{}", sig_mtime=100.0, sig_size=1000)
    assert db.media_probe_is_current("/m.mkv", 100.5, 1000) is True   # within 1s
    assert db.media_probe_is_current("/m.mkv", 102.0, 1000) is False  # outside 1s
    assert db.media_probe_is_current("/m.mkv", 100.0, 999) is False   # size changed


def test_media_probe_is_current_no_row_is_false(tmp_path):
    db = _db(tmp_path)
    assert db.media_probe_is_current("/no/such.mkv", 100.0, 1000) is False


def test_upsert_media_probe_overwrites_on_reprobe(tmp_path):
    db = _db(tmp_path)
    db.upsert_media_probe("/m.mkv", '{"v": 1}', sig_mtime=1.0, sig_size=10)
    db.upsert_media_probe("/m.mkv", '{"v": 2}', sig_mtime=2.0, sig_size=20)
    row = db.get_media_probe("/m.mkv")
    assert json.loads(row["probe_json"]) == {"v": 2}
    assert row["sig_mtime"] == 2.0
