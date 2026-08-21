"""Round 15 M15-1: the journal must fail closed when it cannot be trusted.

Round 15 fixed the HEALTHY-journal restart case. The reviewer's point is that the
fallback still lost restart safety in exactly the situations the fallback exists
for:

    journal write fails   -> the interlock lived only in that process's memory,
                             and the next process started clean
    torn PENDING line     -> skipped as harmless, resurrecting the authority it
                             was written to withdraw
    unreadable journal    -> logged, returned empty, treated as "nothing pending"

The fix inverts the question. Instead of asking "is there a pending revocation?"
-- which a failed write cannot answer -- a process records SESSION_OPEN on start
and SESSION_CLOSED only when nothing is outstanding. A process whose storage
stopped accepting writes cannot write its own close record, so its silence is
itself the signal.

None of these tests seed a valid journal. They break things and restart.
"""
import io
import os
import pytest

from backend.database import DatabaseManager
from backend.download_links import annotate_source_links

URL = "https://hdencode.example/the-release-2026-2160p/"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "r16j.db")


def _identity(db, url=URL):
    rows = [{"id": 1, "provenance_url": url, "provenance_observed": True}]
    annotate_source_links(db, rows)
    return rows[0].get("identity_kind")


def _seed(db, url=URL):
    db.add_to_history(url, "The Release", None, None, "2160p", "20 GB",
                      hdr="HDR", dovi=False, year=2026, media_kind="movie")


def _journal(db):
    return db._revocation_journal_path()


class TestAJournalWriteFailureSurvivesRestart:
    """1A. The interlock used to be per-process, so a crash after a failed
    journal write resurrected the authority on the next start."""

    def test_the_next_process_still_refuses_to_serve(self, db_path, monkeypatch):
        first = DatabaseManager(db_path)
        _seed(first)
        assert _identity(first) == "movie", "precondition: authority is live"

        # Storage stops accepting writes: BOTH the journal and the database.
        monkeypatch.setattr(first, "_journal_append", lambda *a, **k: False)
        def boom(*a, **k):
            raise RuntimeError("injected: storage is gone")
        monkeypatch.setattr(first, "transaction", boom)

        with pytest.raises(Exception):
            first.record_classification_conflicts_and_retract_kinds(
                [URL], reason="classification_conflict")
        assert first._authority_disabled is True
        first.close()          # must NOT record a clean close
        monkeypatch.undo()

        second = DatabaseManager(db_path)
        try:
            urls, healthy, why = second.scan_revocation_journal()
            assert healthy is False, (
                "the previous session left no trace of its failure, so this "
                "process is about to serve the stale authority again")
            second.reconcile_unrevoked_conflicts()
            assert _identity(second) == "unknown"
        finally:
            second.close()

    def test_a_clean_session_leaves_nothing_to_fear(self, db_path):
        """POSITIVE CONTROL. If every restart interlocked, the mechanism would be
        indistinguishable from simply disabling the feature."""
        first = DatabaseManager(db_path)
        _seed(first)
        first.close()

        second = DatabaseManager(db_path)
        try:
            urls, healthy, why = second.scan_revocation_journal()
            assert healthy is True, why
            assert second.reconcile_unrevoked_conflicts() == 0
            assert _identity(second) == "movie", (
                "a clean shutdown must not cost the library its identities")
        finally:
            second.close()


class TestATornJournalFailsClosed:
    """1B. A torn PENDING and a torn DONE are indistinguishable, and skipping a
    torn PENDING restores a withdrawn permission."""

    def test_a_truncated_line_interlocks_the_next_start(self, db_path):
        first = DatabaseManager(db_path)
        _seed(first)
        first.close()

        # Simulate a crash mid-append: a partial JSON line at the end.
        with io.open(_journal(first), "a", encoding="utf-8") as fh:
            fh.write('{"kind": "PENDING", "op": "abc", "urls": ["' + URL)

        second = DatabaseManager(db_path)
        try:
            urls, healthy, why = second.scan_revocation_journal()
            assert healthy is False
            assert "malformed" in why
            second.reconcile_unrevoked_conflicts()
            assert _identity(second) == "unknown", (
                "a torn line was treated as harmless, so a revocation that may "
                "never have completed was forgotten")
        finally:
            second.close()


class TestAnUnreadableJournalFailsClosed:

    def test_a_directory_where_the_journal_should_be(self, db_path):
        """An unreadable journal is not an empty one."""
        first = DatabaseManager(db_path)
        _seed(first)
        path = _journal(first)
        first.close()
        os.remove(path)
        os.makedirs(path)          # opening this raises, it is not a file

        second = DatabaseManager(db_path)
        try:
            urls, healthy, why = second.scan_revocation_journal()
            assert healthy is False
            second.reconcile_unrevoked_conflicts()
            assert _identity(second) == "unknown"
        finally:
            second.close()
