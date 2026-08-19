"""The label sync annotates Plex identity. It does not author observations.

Peer review 2026-08-15 (#72). Making the back-write gentler (observed=False,
preserving signature and timestamp) was not enough, because it still carried a
``dv_layer`` taken from a snapshot at the START of the sync:

    T0  sync snapshots  P = FEL / sig1 / t0
    T1  detector writes P = MEL / sig2 / t1
    T2  sync annotates  P with the stale FEL

leaving dv_layer=FEL beside signature=sig2 and last_seen_at=t1 -- contradictory
evidence, a consumer having erased part of a newer producer observation.
Preserving the timestamp actually SHARPENED the contradiction, which is why the
annotation became UPDATE-only rather than merely gentler.

Authority split these tests pin:

    detector / import : dv_layer, signature, observation freshness
    Plex labeler      : rating_key, and nothing else
"""
import pytest

from backend.database import DatabaseManager

PATH = "/lib/authority.mkv"


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


class TestAnnotationCannotOverwriteAnObservation:
    def test_the_documented_race_leaves_the_NEWER_observation_intact(self, db):
        """The exact sequence from the review, end to end."""
        # T0 -- what the sync would have snapshotted
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=1)
        # T1 -- a detector import lands while the sync is running
        db.upsert_dv_scan(PATH, "mel", sig_mtime=2000.0, sig_size=2)
        # T2 -- the sync annotates, carrying its stale snapshot
        db.annotate_dv_scan_rating_key(PATH, "42")

        row = db.get_dv_scan(PATH)
        assert row["dv_layer"] == "mel", "a consumer must not restore a stale layer"
        assert row["sig_mtime"] == 2000.0
        assert row["sig_size"] == 2
        assert row["rating_key"] == "42", "the annotation itself must still apply"

    def test_annotation_does_not_touch_freshness(self, db):
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=1)
        db._mutate("UPDATE dv_scan SET last_seen_at = ? WHERE path = ?",
                   ("2020-01-01 00:00:00", PATH), label="test_age")
        before = db.get_latest_dv_scan_at(source="scan")

        db.annotate_dv_scan_rating_key(PATH, "42")

        assert db.get_dv_scan(PATH)["last_seen_at"] == "2020-01-01 00:00:00"
        assert db.get_latest_dv_scan_at(source="scan") == before, \
            "annotating must not re-arm the scheduled sync's own gate"

    def test_annotation_does_not_INSERT_a_missing_row(self, db):
        """A row no producer has written is not the labeler's to create;
        inserting one would invent an observation with no layer and no
        signature."""
        assert db.annotate_dv_scan_rating_key("/lib/never-observed.mkv", "99") is False
        assert db.get_dv_scan("/lib/never-observed.mkv") is None

    def test_a_genuine_observation_still_updates_everything(self, db):
        """Control: the producer path must keep full authority, or this fix
        would quietly freeze dv_scan."""
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=1)
        db.annotate_dv_scan_rating_key(PATH, "42")

        db.upsert_dv_scan(PATH, "profile8", sig_mtime=3000.0, sig_size=3)

        row = db.get_dv_scan(PATH)
        assert row["dv_layer"] == "profile8" and row["sig_mtime"] == 3000.0
        assert row["rating_key"] == "42", "the annotation must survive a rescan"
