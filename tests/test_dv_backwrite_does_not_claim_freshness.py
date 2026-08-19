"""The label sync's rating_key back-write must not claim it observed a file.

Found by adversarial review on 2026-08-15, confirmed against production: the
scheduled DV auto-sync gates itself on MAX(last_seen_at) for source='scan'
(``get_latest_dv_scan_at``) and records a PRE-sync watermark. But ``sync_labels``
back-writes every matched movie's row through ``upsert_dv_scan``, which stamped
``last_seen_at = CURRENT_TIMESTAMP`` unconditionally. So each sync pushed the
gate metric past its own watermark and the next hourly pass re-fired -- a full
library reconcile every hour, forever, from one real detection.

Live evidence when this was written: 11 syncs in 14 hours against a detector
that runs every 6 hours, three of them adding zero labels.

The same write also blanked ``sig_mtime``/``sig_size`` (the change-signal),
because the back-write passes no signature and the upsert took the incoming
NULLs -- deliberate for a FAILED host scan, wrong for an annotation that read
no media at all.

Every test here pins ``last_seen_at`` to a fixed past value first. Relying on
wall-clock movement would be vacuous: SQLite's CURRENT_TIMESTAMP has one-second
granularity, so a broken implementation writing "now" twice inside the same
second is indistinguishable from one that preserved the value.
"""
import pytest

from backend.database import DatabaseManager

PAST = "2020-01-01 00:00:00"
PATH = "/lib/backwrite.mkv"


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


def _age_the_row(db, path=PATH):
    """Force a known, distinguishable last_seen_at and a real signature."""
    db._mutate(
        "UPDATE dv_scan SET last_seen_at = ?, sig_mtime = ?, sig_size = ? "
        "WHERE path = ?", (PAST, 1000.0, 500, path), label="test_age")


def _row(db, path=PATH):
    return db.get_dv_scan(path)


class TestBackWriteFreshness:
    def test_unobserved_write_preserves_last_seen_at(self, db):
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=500)
        _age_the_row(db)

        db.upsert_dv_scan(PATH, "fel", rating_key="42", source="scan",
                          observed=False)

        row = _row(db)
        # The write MUST have taken effect -- otherwise "preserved" would be
        # satisfied by a statement that silently did nothing.
        assert row["rating_key"] == "42"
        assert row["last_seen_at"] == PAST

    def test_unobserved_write_preserves_the_signature(self, db):
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=500)
        _age_the_row(db)

        db.upsert_dv_scan(PATH, "fel", rating_key="42", source="scan",
                          observed=False)

        row = _row(db)
        assert row["sig_mtime"] == 1000.0
        assert row["sig_size"] == 500
        # The consumer of that signature must still consider the row current.
        assert db.dv_scan_is_current(PATH, 1000.0, 500) is True

    def test_observed_write_still_refreshes_positive_control(self, db):
        """Without this the suite would pass on an upsert that never updates
        last_seen_at at all -- which would break the detector's own freshness."""
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=500)
        _age_the_row(db)

        db.upsert_dv_scan(PATH, "mel", sig_mtime=2000.0, sig_size=600)

        row = _row(db)
        assert row["last_seen_at"] != PAST
        assert row["sig_mtime"] == 2000.0
        assert row["dv_layer"] == "mel"

    def test_the_gate_itself_does_not_advance(self, db):
        """The axis the bug is on: the scheduled sync reads this exact value.

        A back-write that leaves get_latest_dv_scan_at where it was cannot
        re-arm the hourly trigger; one that bumps it re-fires forever.
        """
        db.upsert_dv_scan(PATH, "fel", sig_mtime=1000.0, sig_size=500)
        _age_the_row(db)
        before = db.get_latest_dv_scan_at(source="scan")

        db.upsert_dv_scan(PATH, "fel", rating_key="42", source="scan",
                          observed=False)
        assert db.get_latest_dv_scan_at(source="scan") == before

        # Control: a genuine detection MUST advance it, or the gate would never
        # fire and DV labels would stop reaching Plex entirely.
        db.upsert_dv_scan(PATH, "mel", sig_mtime=2000.0, sig_size=600)
        assert db.get_latest_dv_scan_at(source="scan") != before
