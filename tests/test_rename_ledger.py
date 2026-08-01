"""The file-operation ledger — safety-gate step 2.

The property under test is not "it logs things". It is that an operation
interrupted mid-flight leaves a durable, findable trace — the one thing
`rename_jobs` structurally cannot provide.
"""

import errno
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.rename.failure import DiskOutcome, Phase, classify_failure
from backend.rename.ledger import (
    FileOpLedger,
    LedgerWriteError,
)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "ledger.db")
    DatabaseManager(path)
    c = sqlite3.connect(path)
    yield c
    c.close()


@pytest.fixture
def ledger(conn):
    return FileOpLedger(conn)


def place(ledger, src="/dl/a.mkv", dst="/library/a.mkv", job_id=1):
    return ledger.record_intent(operation="place", src=src, dst=dst,
                                method="move", job_id=job_id)


class TestIntentIsRecordedFirst:
    def test_intent_is_committed_before_any_outcome_exists(self, ledger, conn):
        """The intent must survive the very crash it exists to record, so it is
        committed on its own rather than as part of a later transaction."""
        uid = place(ledger)
        fresh = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
        try:
            n = fresh.execute(
                "SELECT COUNT(*) FROM fileop_events WHERE event_uuid=? AND kind='intent'",
                (uid,)).fetchone()[0]
        finally:
            fresh.close()
        assert n == 1        # visible to a separate connection => committed

    def test_a_failed_intent_write_raises_rather_than_returning(self, conn):
        """FAIL-CLOSED. The caller must be unable to proceed with a file
        operation it could not record. SH-R03 is the precedent: a 'best-effort'
        manifest write whose loss made restore impossible."""
        conn.execute("DROP TABLE fileop_events")
        conn.commit()
        with pytest.raises(LedgerWriteError):
            place(FileOpLedger(conn))

    def test_the_error_names_the_paths_involved(self, conn):
        conn.execute("DROP TABLE fileop_events")
        conn.commit()
        with pytest.raises(LedgerWriteError) as e:
            FileOpLedger(conn).record_intent(operation="place", src="/dl/x.mkv",
                                             dst="/library/x.mkv")
        assert "/dl/x.mkv" in str(e.value) and "/library/x.mkv" in str(e.value)


class TestInterruptionDetection:
    def test_an_intent_with_no_outcome_reads_as_interrupted(self, ledger):
        """THE POINT OF THE TABLE. This is the state rename_jobs cannot express:
        the job row just sits in its last status, saying nothing about a move
        that was in flight."""
        place(ledger, src="/dl/interrupted.mkv", dst="/library/interrupted.mkv")
        stuck = ledger.interrupted_operations()
        assert len(stuck) == 1
        assert stuck[0].src_path == "/dl/interrupted.mkv"
        assert "never reported an outcome" in stuck[0].summary

    def test_a_completed_operation_is_not_interrupted(self, ledger):
        uid = place(ledger)
        ledger.record_success(uid, method_used="copy")
        assert ledger.interrupted_operations() == []

    def test_a_FAILED_operation_is_not_interrupted_either(self, ledger):
        """A recorded failure is a known state. Only silence is interruption."""
        uid = place(ledger)
        ledger.record_failure(uid, classify_failure(OSError(errno.ENOSPC, "full"),
                                                    bytes_written=999))
        assert ledger.interrupted_operations() == []

    def test_multiple_interruptions_come_back_in_time_order(self, ledger):
        import datetime as dt
        base = dt.datetime(2026, 8, 1, 12, 0, 0)
        ledger.record_intent(operation="place", src="/b", now=base + dt.timedelta(minutes=5))
        ledger.record_intent(operation="place", src="/a", now=base)
        assert [o.src_path for o in ledger.interrupted_operations()] == ["/a", "/b"]


class TestAppendOnly:
    def test_the_outcome_is_a_SEPARATE_row(self, ledger, conn):
        """Never an UPDATE: a bug in the outcome path must not be able to
        destroy the record of intent, which is the record that matters when
        something has gone wrong."""
        uid = place(ledger)
        ledger.record_success(uid)
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM fileop_events WHERE event_uuid=? ORDER BY id", (uid,))]
        assert kinds == ["intent", "outcome"]

    def test_the_intent_row_is_unchanged_after_an_outcome(self, ledger, conn):
        uid = place(ledger)
        before = conn.execute(
            "SELECT src_path,dst_path,method,succeeded FROM fileop_events "
            "WHERE event_uuid=? AND kind='intent'", (uid,)).fetchone()
        ledger.record_failure(uid, classify_failure(RuntimeError("?")))
        after = conn.execute(
            "SELECT src_path,dst_path,method,succeeded FROM fileop_events "
            "WHERE event_uuid=? AND kind='intent'", (uid,)).fetchone()
        assert before == after
        assert before[3] is None       # intent rows never carry an outcome

    def test_the_outcome_inherits_the_paths_from_its_intent(self, ledger, conn):
        """So a single row is self-describing in an audit, without a join."""
        uid = place(ledger, src="/dl/z.mkv", dst="/library/z.mkv")
        ledger.record_success(uid)
        row = conn.execute(
            "SELECT src_path,dst_path,job_id FROM fileop_events "
            "WHERE event_uuid=? AND kind='outcome'", (uid,)).fetchone()
        assert row == ("/dl/z.mkv", "/library/z.mkv", 1)


class TestOutcomeCarriesTheSafetyBucket:
    def test_the_step_one_verdict_is_stored_verbatim(self, ledger, conn):
        """The bucket recorded must be the one the safety rules were evaluated
        against — re-deriving it here could let the two drift apart."""
        verdict = classify_failure(OSError(errno.ENOSPC, "full"), bytes_written=1024)
        uid = place(ledger)
        ledger.record_failure(uid, verdict)
        cause, disk = conn.execute(
            "SELECT cause, disk_outcome FROM fileop_events "
            "WHERE event_uuid=? AND kind='outcome'", (uid,)).fetchone()
        assert cause == verdict.cause.value
        assert disk == verdict.disk_outcome.value == DiskOutcome.DEST_PARTIAL.value

    def test_the_dangerous_buckets_survive_into_the_ledger(self, ledger, conn):
        uid = place(ledger)
        ledger.record_failure(uid, classify_failure(
            RuntimeError("db write failed"), phase=Phase.POST_PLACEMENT_RECORD))
        disk = conn.execute(
            "SELECT disk_outcome FROM fileop_events WHERE event_uuid=? AND kind='outcome'",
            (uid,)).fetchone()[0]
        assert disk == DiskOutcome.MOVED_UNRECORDED.value


class TestCounting:
    def test_counts_are_per_bucket_not_free_text(self, ledger):
        ok = place(ledger, job_id=1)
        ledger.record_success(ok)
        partial = place(ledger, job_id=2)
        ledger.record_failure(partial, classify_failure(
            OSError(errno.EIO, "io"), bytes_written=5))
        counts = ledger.outcome_counts()
        assert counts["succeeded"] == 1
        assert counts[DiskOutcome.DEST_PARTIAL.value] == 1

    def test_interrupted_operations_are_counted_not_omitted(self, ledger):
        """An omitted bucket is one nobody notices. The totals must reconcile
        against the number of intents."""
        ledger.record_success(place(ledger, job_id=1))
        place(ledger, job_id=2)          # left in flight
        counts = ledger.outcome_counts()
        assert counts["interrupted"] == 1
        assert sum(counts.values()) == 2


class TestJobHistory:
    def test_a_job_can_be_replayed_in_order(self, ledger):
        uid = place(ledger, job_id=7)
        ledger.record_failure(uid, classify_failure(OSError(errno.EACCES, "denied")))
        retry = place(ledger, job_id=7)
        ledger.record_success(retry)
        history = ledger.history_for_job(7)
        assert [r[0] for r in history] == ["intent", "outcome", "intent", "outcome"]
        assert history[1][7] == "permission_denied"     # cause column
