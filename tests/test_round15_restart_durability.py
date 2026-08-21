"""Round 14 review, M14-1: the journal must survive the failure it exists for.

My round-13 restart tests were circular and the reviewer caught it. They seeded

    cache.category_conflict = true
    downloads.media_kind    = movie

by hand and proved startup recovery handles that state. What they never proved is
that the state EXISTS after the failure it is supposed to describe. Recovery
depended on writing a conflict mark to the same SQLite database whose erase had
just been refused -- so in exactly the case it mattered, no mark was written,
startup saw no interrupted-revocation signature, and the stale authority came
back.

So these tests never seed the journal. They break the database, let production
fail however it fails, then construct a NEW DatabaseManager on the same path --
a real restart, with empty in-memory holds -- and ask whether authority is
withdrawn.
"""
import pytest

from backend.database import DatabaseManager
from backend.download_links import annotate_source_links

URL = "https://hdencode.example/the-release-2026-2160p/"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "r15.db")


def _identity(db, url=URL):
    rows = [{"id": 1, "provenance_url": url, "provenance_observed": True}]
    annotate_source_links(db, rows)
    return rows[0].get("identity_kind")


def _seed(db, url=URL):
    db.add_to_history(url, "The Release", None, None, "2160p", "20 GB",
                      hdr="HDR", dovi=False, year=2026, media_kind="movie")


class TestARestartAfterATotalWriteFailure:

    def test_authority_is_withdrawn_after_restart(self, db_path):
        """THE REQUIRED CASE. Both the erase and the conflict-mark write fail,
        the process dies, and a fresh one starts against the same files."""
        first = DatabaseManager(db_path)
        _seed(first)
        assert _identity(first) == "movie", "precondition: authority is live"

        def boom(*a, **k):
            raise RuntimeError("injected: database is locked")
        original = first.transaction
        first.transaction = boom
        with pytest.raises(Exception):
            first.record_classification_conflicts_and_retract_kinds(
                [URL], reason="classification_conflict")
        first.transaction = original
        # The database is untouched -- neither write landed.
        row = first._query("SELECT media_kind FROM downloads WHERE url = ?",
                           (URL,), one=True, default=None)
        assert dict(row).get("media_kind") == "movie"
        first.close()

        # RESTART. Nothing is carried over in memory.
        second = DatabaseManager(db_path)
        try:
            assert second.is_media_kind_held(URL) is False, (
                "fixture error: the new process must start with no holds")
            assert second.reconcile_unrevoked_conflicts() >= 1, (
                "startup found no record of the interrupted revocation, so the "
                "stale authority is about to be served again")
            assert _identity(second) == "unknown"
        finally:
            second.close()

    def test_a_completed_revocation_leaves_nothing_to_recover(self, db_path):
        """POSITIVE CONTROL. If recovery fired on every restart regardless, the
        test above would pass while the mechanism was meaningless -- and every
        restart would wipe authority from the whole library."""
        first = DatabaseManager(db_path)
        _seed(first)
        first.record_classification_conflicts_and_retract_kinds(
            [URL], reason="classification_conflict")
        first.close()

        second = DatabaseManager(db_path)
        try:
            assert second.reconcile_unrevoked_conflicts() == 0, (
                "a completed revocation was replayed as if unfinished")
        finally:
            second.close()

    def test_an_untouched_database_recovers_nothing(self, db_path):
        first = DatabaseManager(db_path)
        _seed(first)
        first.close()
        second = DatabaseManager(db_path)
        try:
            assert second.reconcile_unrevoked_conflicts() == 0
            assert _identity(second) == "movie", (
                "recovery withdrew authority from a release that never had a "
                "conflict")
        finally:
            second.close()


class TestWhenTheJournalItselfCannotBeWritten:
    """The irreducible case, and the honest answer to it.

    Nothing can be durable if the disk refuses every write. What the system can
    do is stop making promises it cannot keep."""

    def test_authority_is_disabled_process_wide(self, db_path, monkeypatch):
        db = DatabaseManager(db_path)
        try:
            _seed(db)
            assert _identity(db) == "movie"

            def no_journal(*a, **k):
                return False
            monkeypatch.setattr(db, "_journal_append", no_journal)

            def boom(*a, **k):
                raise RuntimeError("injected: database is locked")
            monkeypatch.setattr(db, "transaction", boom)
            with pytest.raises(Exception):
                db.record_classification_conflicts_and_retract_kinds(
                    [URL], reason="classification_conflict")

            monkeypatch.undo()
            assert db._authority_disabled is True
            assert _identity(db) == "unknown", (
                "the journal could not be written, so a restart cannot recover "
                "-- this process must stop serving identities entirely")
        finally:
            db.close()

    def test_an_unrelated_release_is_also_withheld(self, db_path, monkeypatch):
        """Deliberately broad. The interlock is not per-release: we do not know
        what else was mid-flight, so nothing is vouched for."""
        other = "https://hdencode.example/another/"
        db = DatabaseManager(db_path)
        try:
            _seed(db)
            _seed(db, url=other)
            db._authority_disabled = True
            assert _identity(db, other) == "unknown"
        finally:
            db.close()
