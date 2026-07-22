"""Tests for the dv_scan DB layer (Dolby Vision layer inventory)."""
import pytest

from backend.database import DatabaseManager


@pytest.fixture(autouse=True)
def _reset():
    def _clear():
        try:
            dm = DatabaseManager(); dm.clear_dv_scans(); dm.close()
        except Exception:
            pass
    _clear(); yield; _clear()


@pytest.fixture
def db():
    dm = DatabaseManager(); yield dm; dm.close()


class TestDvScanDB:
    def test_upsert_and_get(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", title="A",
                          sig_mtime=1000.0, sig_size=42, source="scan")
        row = db.get_dv_scan("/lib/A.mkv")
        assert row["dv_layer"] == "fel"
        assert row["title"] == "A"
        assert row["source"] == "scan"

    def test_upsert_is_idempotent_by_path(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "mel", title="A", sig_mtime=1.0, sig_size=1)
        db.upsert_dv_scan("/lib/A.mkv", "fel", title="A", sig_mtime=2.0, sig_size=2)
        rows = db.get_dv_scans()
        assert len(rows) == 1 and rows[0]["dv_layer"] == "fel"

    def test_upsert_preserves_title_when_null(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", title="Original", sig_size=1, sig_mtime=1.0)
        # A later scan without a title must not wipe the existing one.
        db.upsert_dv_scan("/lib/A.mkv", "mel", title=None, sig_size=2, sig_mtime=2.0)
        assert db.get_dv_scan("/lib/A.mkv")["title"] == "Original"

    def test_filter_by_layer(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", sig_mtime=1.0, sig_size=1)
        db.upsert_dv_scan("/lib/B.mkv", "mel", sig_mtime=1.0, sig_size=1)
        db.upsert_dv_scan("/lib/C.mkv", "fel", sig_mtime=1.0, sig_size=1)
        fel = db.get_dv_scans(dv_layer="fel")
        assert {r["path"] for r in fel} == {"/lib/A.mkv", "/lib/C.mkv"}

    def test_count_by_layer(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", sig_mtime=1.0, sig_size=1)
        db.upsert_dv_scan("/lib/B.mkv", "fel", sig_mtime=1.0, sig_size=1)
        db.upsert_dv_scan("/lib/C.mkv", "none", sig_mtime=1.0, sig_size=1)
        assert db.count_dv_scans_by_layer() == {"fel": 2, "none": 1}

    def test_is_current_skips_unchanged(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", sig_mtime=1000.0, sig_size=500)
        assert db.dv_scan_is_current("/lib/A.mkv", 1000.0, 500) is True
        # Changed size → not current (must re-scan).
        assert db.dv_scan_is_current("/lib/A.mkv", 1000.0, 999) is False
        # Changed mtime → not current.
        assert db.dv_scan_is_current("/lib/A.mkv", 2000.0, 500) is False

    def test_is_current_false_for_unscanned(self, db):
        assert db.dv_scan_is_current("/lib/never.mkv", 1.0, 1) is False

    def test_is_current_false_when_signature_missing(self, db):
        # A seed row without a signature must always be (re)scannable.
        db.upsert_dv_scan("/lib/seed.mkv", "fel", source="seed",
                          sig_mtime=None, sig_size=None)
        assert db.dv_scan_is_current("/lib/seed.mkv", 1.0, 1) is False

    def test_get_dv_scans_by_paths_bulk(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", title="A", sig_mtime=1.0, sig_size=10)
        db.upsert_dv_scan("/lib/B.mkv", "mel", title="B", sig_mtime=2.0, sig_size=20)
        result = db.get_dv_scans_by_paths(["/lib/A.mkv", "/lib/B.mkv", "/lib/missing.mkv"])
        assert set(result.keys()) == {"/lib/A.mkv", "/lib/B.mkv"}
        assert result["/lib/A.mkv"].get("dv_layer") == "fel"
        assert result["/lib/B.mkv"].get("dv_layer") == "mel"
        assert "/lib/missing.mkv" not in result

    def test_get_dv_scans_by_paths_empty_input(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", sig_mtime=1.0, sig_size=10)
        assert db.get_dv_scans_by_paths([]) == {}
        assert db.get_dv_scans_by_paths(None) == {}


    def test_latest_dv_scan_tracks_updates_to_existing_path(self, db):
        """A rescan that changes an existing row must wake scheduled label sync."""
        db.upsert_dv_scan("/lib/a.mkv", "mel", source="scan")
        with db._lock:
            db.conn.execute(
                "UPDATE dv_scan SET scanned_at='2026-01-01 00:00:00', "
                "last_seen_at='2026-01-01 00:00:00' WHERE path='/lib/a.mkv'"
            )
            db.conn.commit()

        before = db.get_latest_dv_scan_at(source="scan")
        db.upsert_dv_scan("/lib/a.mkv", "fel", source="scan")
        after = db.get_latest_dv_scan_at(source="scan")

        assert before == "2026-01-01 00:00:00"
        assert after > before
