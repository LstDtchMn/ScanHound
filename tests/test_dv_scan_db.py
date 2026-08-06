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


class TestFailedDetectionNeverDestroysAGoodLayer:
    """'unknown' means detection FAILED (dv_detect resolves every error to it).

    The host scanner writes such a row with a NULL signature so the next run
    retries -- but the upsert overwrote the layer unconditionally, so one
    unreadable file on a network mount replaced a real 'fel' with 'unknown',
    and the labeler then had no evidence to keep the Kometa overlay labels.
    The same statement already COALESCE-preserves title/rating_key/imdb_id.
    """

    def test_unknown_does_not_overwrite_a_known_layer(self, db):
        db.upsert_dv_scan("/lib/A.mkv", "fel", title="A",
                          sig_mtime=1000.0, sig_size=42, source="scan")
        db.upsert_dv_scan("/lib/A.mkv", "unknown", source="scan")
        row = db.get_dv_scan("/lib/A.mkv")
        assert row["dv_layer"] == "fel"

    def test_the_retry_signature_still_lands(self, db):
        # The NULL sig is what makes the next host run re-detect; preserving
        # the layer must not also preserve a stale "already scanned" marker.
        db.upsert_dv_scan("/lib/B.mkv", "mel", sig_mtime=1000.0, sig_size=42,
                          source="scan")
        db.upsert_dv_scan("/lib/B.mkv", "unknown", sig_mtime=None,
                          sig_size=None, source="scan")
        row = db.get_dv_scan("/lib/B.mkv")
        assert row["dv_layer"] == "mel"
        assert row["sig_mtime"] is None and row["sig_size"] is None

    def test_a_real_layer_change_still_applies(self, db):
        # Negative control: authoritative findings must still overwrite.
        db.upsert_dv_scan("/lib/C.mkv", "mel", source="scan")
        db.upsert_dv_scan("/lib/C.mkv", "fel", source="scan")
        assert db.get_dv_scan("/lib/C.mkv")["dv_layer"] == "fel"

    def test_authoritative_none_still_applies(self, db):
        # 'none' is a finding ("the tool ran, there is no DV"), not a failure.
        db.upsert_dv_scan("/lib/D.mkv", "fel", source="scan")
        db.upsert_dv_scan("/lib/D.mkv", "none", source="scan")
        assert db.get_dv_scan("/lib/D.mkv")["dv_layer"] == "none"

    def test_first_write_of_unknown_is_stored(self, db):
        # Nothing to preserve: a fresh failure must still be recorded so the
        # row exists with its NULL signature for the retry.
        db.upsert_dv_scan("/lib/E.mkv", "unknown", source="scan")
        assert db.get_dv_scan("/lib/E.mkv")["dv_layer"] == "unknown"
