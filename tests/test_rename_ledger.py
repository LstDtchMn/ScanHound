"""The file-operation ledger — safety-gate step 2 (rev 2).

The property under test is not "it logs things". It is that an operation
interrupted mid-flight leaves a durable trace that can be RECONCILED against
the filesystem — the thing `rename_jobs` structurally cannot provide.

Rev 2 exists because review found three faults in rev 1: a silent-failure path,
a model enforced only by convention, and an intent record too thin to resolve
anything. Each has a test below whose name says which.
"""

import errno
import os
import sqlite3

import pytest

from backend.database import DatabaseManager
from backend.rename.failure import (
    DiskObservation,
    DiskOutcome,
    Phase,
    classify_failure,
)
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


@pytest.fixture
def real_files(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 2048)
    return src, tmp_path / "dst.mkv"


def place(ledger, src="/dl/a.mkv", dst="/library/a.mkv", job_id=1, **kw):
    return ledger.record_intent(operation="place", src=src, dst=dst,
                                method="move", job_id=job_id, **kw)


class TestIntentIsRecordedFirst:
    def test_intent_is_committed_before_any_outcome_exists(self, ledger, conn):
        """The intent must survive the very crash it exists to record."""
        uid = place(ledger)
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        fresh = sqlite3.connect(path)
        try:
            n = fresh.execute(
                "SELECT COUNT(*) FROM fileop_events "
                "WHERE operation_uuid=? AND kind='intent'", (uid,)).fetchone()[0]
        finally:
            fresh.close()
        assert n == 1

    def test_a_failed_intent_write_raises_rather_than_returning(self, conn):
        """FAIL-CLOSED. SH-R03 is the precedent: a best-effort write whose loss
        made restore impossible."""
        conn.execute("DROP TABLE fileop_events")
        conn.commit()
        with pytest.raises(LedgerWriteError):
            place(FileOpLedger(conn))


class TestSilentFailureIsFixed:
    """REV 1 FAULT 1, reproduced before fixing: `_record_outcome` used
    INSERT...SELECT, so an operation uuid with no intent inserted ZERO rows,
    committed, and returned normally. The module whose entire purpose is
    durable bookkeeping recorded nothing and reported success."""

    def test_an_outcome_for_a_NONEXISTENT_intent_raises(self, ledger):
        with pytest.raises(LedgerWriteError) as e:
            ledger.record_success("no-such-operation-uuid")
        assert "0 row(s) written" in str(e.value)

    def test_the_same_for_a_failure_outcome(self, ledger):
        verdict = classify_failure(OSError(errno.ENOSPC, "full"))
        with pytest.raises(LedgerWriteError):
            ledger.record_failure("also-not-real", verdict)

    def test_nothing_is_left_behind_by_the_rejected_write(self, ledger, conn):
        with pytest.raises(LedgerWriteError):
            ledger.record_success("nope")
        assert conn.execute(
            "SELECT COUNT(*) FROM fileop_events").fetchone()[0] == 0

    def test_a_SECOND_outcome_for_one_operation_raises(self, ledger):
        """A terminal outcome is not an update."""
        uid = place(ledger)
        ledger.record_success(uid)
        with pytest.raises(LedgerWriteError) as e:
            ledger.record_success(uid)
        assert "already exists" in str(e.value)


class TestTheModelIsDatabaseEnforced:
    """REV 1 FAULT 2: the shape was a convention the writer was trusted to
    honour. A ledger whose shape depends on its writer being correct is not
    evidence."""

    def test_two_intents_for_one_operation_are_REFUSED(self, conn):
        conn.execute(
            "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
            "recorded_at, operation) VALUES ('e1','op1','intent','t','place')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
                "recorded_at, operation) VALUES ('e2','op1','intent','t','place')")

    def test_two_outcomes_for_one_operation_are_REFUSED(self, conn):
        conn.execute(
            "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
            "recorded_at, operation) VALUES ('e1','op1','outcome','t','place')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
                "recorded_at, operation) VALUES ('e2','op1','outcome','t','place')")

    def test_an_arbitrary_kind_is_REFUSED(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
                "recorded_at, operation) VALUES ('e1','op1','whatever','t','x')")

    def test_a_duplicate_event_uuid_is_REFUSED(self, conn):
        conn.execute(
            "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
            "recorded_at, operation) VALUES ('e1','op1','intent','t','place')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fileop_events (event_uuid, operation_uuid, kind, "
                "recorded_at, operation) VALUES ('e1','op2','intent','t','place')")


class TestPreOperationEvidence:
    """REV 1 FAULT 3: a bare marker cannot distinguish a crash before the
    operation from one during or after it. All three read as "intent with no
    outcome"."""

    def test_the_intent_captures_the_source_identity(self, ledger, conn, real_files):
        src, dst = real_files
        uid = ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                                   method="move")
        row = conn.execute(
            "SELECT src_size, src_mtime, src_inode, dst_existed FROM fileop_events "
            "WHERE operation_uuid=? AND kind='intent'", (uid,)).fetchone()
        assert row[0] == 2048          # size
        assert row[1] is not None      # mtime
        assert row[2] and ":" in row[2]  # dev:inode
        assert row[3] == 0             # destination did not exist

    def test_an_unreadable_source_records_UNKNOWN_not_absent(self, ledger, conn):
        uid = place(ledger, src="/does/not/exist.mkv")
        size = conn.execute(
            "SELECT src_size FROM fileop_events WHERE operation_uuid=?",
            (uid,)).fetchone()[0]
        assert size is None

    def test_reconciliation_finds_an_UNTOUCHED_source(self, ledger, real_files):
        """Crash before the operation began: source as recorded, no destination."""
        src, dst = real_files
        ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                             method="move")
        (_, observation), = ledger.reconcile_interrupted()
        assert classify_failure(None, observation).disk_outcome is \
            DiskOutcome.UNCHANGED

    def test_reconciliation_detects_a_COMPLETED_move(self, ledger, real_files):
        """Crash after the move but before the outcome — previously
        indistinguishable from a crash before it started."""
        src, dst = real_files
        ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                             method="move")
        os.rename(src, dst)
        (_, observation), = ledger.reconcile_interrupted()
        assert classify_failure(None, observation).disk_outcome is \
            DiskOutcome.DEST_COMPLETE_SOURCE_ABSENT

    def test_reconciliation_detects_a_DUPLICATE(self, ledger, real_files):
        """Copy finished, source not consumed."""
        src, dst = real_files
        ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                             method="copy")
        dst.write_bytes(src.read_bytes())
        (_, observation), = ledger.reconcile_interrupted()
        assert classify_failure(None, observation).disk_outcome is \
            DiskOutcome.DEST_COMPLETE_SOURCE_PRESENT

    def test_reconciliation_detects_a_PARTIAL_destination(self, ledger, real_files):
        src, dst = real_files
        ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                             method="copy")
        dst.write_bytes(b"x" * 100)          # truncated
        (_, observation), = ledger.reconcile_interrupted()
        assert classify_failure(None, observation).disk_outcome is \
            DiskOutcome.DEST_PARTIAL_SOURCE_PRESENT

    def test_reconciliation_detects_THE_CATASTROPHIC_STATE(self, ledger, real_files):
        """Source consumed, destination never landed. This is the one that must
        never be reported as merely "interrupted"."""
        src, dst = real_files
        ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                             method="move")
        os.remove(src)
        (_, observation), = ledger.reconcile_interrupted()
        verdict = classify_failure(None, observation)
        assert verdict.disk_outcome is DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE
        assert verdict.is_catastrophic

    def test_a_DIFFERENT_file_at_the_source_path_is_not_the_source(self, ledger,
                                                                   real_files):
        """Same path, different size: reconciliation must not call that intact.
        Treating it as the original is how a reconciliation lies."""
        src, dst = real_files
        ledger.record_intent(operation="place", src=str(src), dst=str(dst),
                             method="move")
        src.write_bytes(b"y" * 99)
        (_, observation), = ledger.reconcile_interrupted()
        assert observation.source_present is False


class TestInterruptionDetection:
    def test_an_intent_with_no_outcome_reads_as_interrupted(self, ledger):
        place(ledger, src="/dl/interrupted.mkv", dst="/library/interrupted.mkv")
        stuck = ledger.interrupted_operations()
        assert len(stuck) == 1
        assert "never reported an outcome" in stuck[0].summary

    def test_a_completed_operation_is_not_interrupted(self, ledger):
        ledger.record_success(place(ledger), method_used="copy")
        assert ledger.interrupted_operations() == []

    def test_a_FAILED_operation_is_not_interrupted_either(self, ledger):
        """A recorded failure is a known state. Only silence is interruption."""
        uid = place(ledger)
        ledger.record_failure(uid, classify_failure(
            OSError(errno.ENOSPC, "full"),
            DiskObservation(source_present=True, destination_present=False)))
        assert ledger.interrupted_operations() == []


class TestAppendOnly:
    def test_the_outcome_is_a_SEPARATE_row(self, ledger, conn):
        uid = place(ledger)
        ledger.record_success(uid)
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM fileop_events WHERE operation_uuid=? ORDER BY id",
            (uid,))]
        assert kinds == ["intent", "outcome"]

    def test_the_intent_row_is_unchanged_after_an_outcome(self, ledger, conn):
        uid = place(ledger)
        before = conn.execute(
            "SELECT src_path,dst_path,method,succeeded FROM fileop_events "
            "WHERE operation_uuid=? AND kind='intent'", (uid,)).fetchone()
        ledger.record_failure(uid, classify_failure(RuntimeError("?")))
        after = conn.execute(
            "SELECT src_path,dst_path,method,succeeded FROM fileop_events "
            "WHERE operation_uuid=? AND kind='intent'", (uid,)).fetchone()
        assert before == after and before[3] is None

    def test_the_outcome_inherits_the_paths_from_its_intent(self, ledger, conn):
        uid = place(ledger, src="/dl/z.mkv", dst="/library/z.mkv")
        ledger.record_success(uid)
        assert conn.execute(
            "SELECT src_path,dst_path,job_id FROM fileop_events "
            "WHERE operation_uuid=? AND kind='outcome'", (uid,)).fetchone() == (
                "/dl/z.mkv", "/library/z.mkv", 1)


class TestOutcomeCarriesTheSafetyBucket:
    def test_the_step_one_verdict_is_stored_verbatim(self, ledger, conn):
        verdict = classify_failure(
            OSError(errno.ENOSPC, "full"),
            DiskObservation(source_present=True, destination_present=True,
                            destination_complete=False))
        uid = place(ledger)
        ledger.record_failure(uid, verdict)
        cause, disk = conn.execute(
            "SELECT cause, disk_outcome FROM fileop_events "
            "WHERE operation_uuid=? AND kind='outcome'", (uid,)).fetchone()
        assert cause == verdict.cause.value
        assert disk == DiskOutcome.DEST_PARTIAL_SOURCE_PRESENT.value

    def test_the_catastrophic_bucket_survives_into_the_ledger(self, ledger, conn):
        uid = place(ledger)
        ledger.record_failure(uid, classify_failure(
            OSError(errno.EIO, "io"),
            DiskObservation(source_present=False, destination_present=False)))
        assert conn.execute(
            "SELECT disk_outcome FROM fileop_events "
            "WHERE operation_uuid=? AND kind='outcome'", (uid,)).fetchone()[0] == \
            DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE.value


class TestCounting:
    def test_interrupted_operations_are_counted_not_omitted(self, ledger):
        ledger.record_success(place(ledger, job_id=1))
        place(ledger, job_id=2)              # left in flight
        counts = ledger.outcome_counts()
        assert counts["interrupted"] == 1
        assert sum(counts.values()) == 2


class TestJobHistory:
    def test_a_job_can_be_replayed_in_order(self, ledger):
        uid = place(ledger, job_id=7)
        ledger.record_failure(uid, classify_failure(
            OSError(errno.EACCES, "denied"),
            DiskObservation(source_present=True, destination_present=False)))
        ledger.record_success(place(ledger, job_id=7))
        history = ledger.history_for_job(7)
        assert [r[0] for r in history] == ["intent", "outcome", "intent", "outcome"]
        assert history[1][7] == "permission_denied"
