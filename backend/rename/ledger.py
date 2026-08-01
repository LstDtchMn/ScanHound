"""Append-only file-operation ledger — safety-gate step 2 (rev 2).

`rename_jobs` records the CURRENT STATE of a job, which is not a history. When
a process dies mid-move the row sits in whatever status it last held; nothing
says a file was being moved from A to B at that moment. That is the one window
where disk and database can silently disagree, and it left no trace.

REV 2 CORRECTS THREE FAULTS IN REV 1, all found in review:

1. **A silent-failure path.** `_record_outcome` used ``INSERT ... SELECT ...
   FROM intent``. Given an operation uuid with no intent row, SQLite inserted
   ZERO rows, committed, and returned normally. Reproduced: the module whose
   entire purpose is durable bookkeeping recorded nothing and reported success.
   Outcomes now verify exactly one row was written and raise otherwise.

2. **The model was a convention, not a constraint.** Nothing stopped two
   intents sharing an operation, two terminal outcomes for one intent, an
   outcome with no intent, or an arbitrary ``kind``. Those are now partial
   unique indexes and a CHECK. A ledger whose shape depends on its writer being
   correct is not evidence.

3. **A bare "started" marker cannot reconcile anything.** Crash before the
   operation, during it, and after it but before the outcome all look identical:
   "intent with no outcome". The intent now carries a PRE-OPERATION EVIDENCE
   SNAPSHOT — source identity, destination pre-state, prior-occupant reference,
   method, expected postcondition — so an interrupted operation can be compared
   against the filesystem and RESOLVED, not merely reported to a human.

Unchanged from rev 1, because it was right: intent is committed BEFORE the
filesystem is touched and the write must succeed. If it cannot be persisted the
operation does not run. Deliberately not best-effort — SH-R03 is the precedent,
where a "degraded" manifest write was in fact permanent, unrecoverable loss.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional

from backend.rename.failure import DiskObservation, FailureVerdict

KIND_INTENT = "intent"
KIND_OUTCOME = "outcome"


class LedgerWriteError(RuntimeError):
    """A ledger write did not durably record what it claimed to."""


@dataclass(frozen=True)
class InterruptedOperation:
    operation_uuid: str
    recorded_at: str
    job_id: Optional[int]
    operation: str
    method: Optional[str]
    src_path: Optional[str]
    dst_path: Optional[str]
    src_size: Optional[int]
    src_mtime: Optional[float]
    src_inode: Optional[str]
    dst_existed: Optional[int]
    prior_occupant_ref: Optional[str]
    temp_path: Optional[str]
    expected_postcondition: Optional[str]

    @property
    def summary(self) -> str:
        return (f"{self.operation}({self.method or '?'}) "
                f"{self.src_path} -> {self.dst_path} "
                f"began {self.recorded_at} and never reported an outcome")

    def observe_now(self) -> DiskObservation:
        """Compare the recorded pre-state against the filesystem NOW.

        This is what the evidence snapshot buys: an interrupted operation
        becomes a classifiable disk state instead of an open question. The
        source counts as PRESENT only if it still looks like the file the intent
        described — a different size or mtime at the same path is not the same
        file, and treating it as one is how a reconciliation lies.
        """
        src_present = None
        if self.src_path:
            try:
                st = os.stat(self.src_path)
                src_present = (
                    (self.src_size is None or st.st_size == self.src_size)
                    and (self.src_mtime is None
                         or abs(st.st_mtime - self.src_mtime) <= 2.0))
            except FileNotFoundError:
                src_present = False
            except OSError:
                src_present = None       # could not tell -> stays unknown

        dst_present = None
        dst_complete = None
        if self.dst_path:
            try:
                dst_stat = os.stat(self.dst_path)
                dst_present = True
                # Complete only if it matches the size the source had. Anything
                # else is UNVERIFIED, which is not the same as complete.
                dst_complete = (self.src_size is not None
                                and dst_stat.st_size == self.src_size)
            except FileNotFoundError:
                dst_present = False
                dst_complete = None
            except OSError:
                dst_present = None

        temp_present = None
        if self.temp_path:
            try:
                temp_present = os.path.exists(self.temp_path)
            except OSError:
                temp_present = None

        return DiskObservation(
            source_present=src_present,
            destination_present=dst_present,
            destination_complete=dst_complete,
            prior_occupant_trashed=bool(self.prior_occupant_ref),
            temp_path_present=temp_present,
            method=self.method,
        )


class FileOpLedger:
    """Append-only record of what the app was about to do, and what happened."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── intent ──────────────────────────────────────────────────────────
    def record_intent(self, *, operation: str, src: Optional[str] = None,
                      dst: Optional[str] = None, method: Optional[str] = None,
                      job_id: Optional[int] = None,
                      prior_occupant_ref: Optional[str] = None,
                      temp_path: Optional[str] = None,
                      expected_postcondition: Optional[str] = None,
                      idempotency_token: Optional[str] = None,
                      now: Optional[dt.datetime] = None) -> str:
        """Capture the pre-operation state and return the operation uuid.

        The caller MUST treat an exception here as "do not perform the file
        operation". An unrecorded operation cannot be recovered from, and
        refusing to start is always cheaper than that.
        """
        operation_uuid = str(uuid.uuid4())
        src_size = src_mtime = src_inode = None
        if src:
            try:
                st = os.stat(src)
                src_size, src_mtime = st.st_size, st.st_mtime
                src_inode = f"{st.st_dev}:{st.st_ino}"
            except OSError:
                pass          # recorded as unknown, never as absent
        dst_existed = None
        if dst:
            try:
                dst_existed = 1 if os.path.exists(dst) else 0
            except OSError:
                dst_existed = None

        try:
            self.conn.execute(
                "INSERT INTO fileop_events "
                "(event_uuid, operation_uuid, kind, recorded_at, job_id, "
                " operation, method, src_path, dst_path, src_size, src_mtime, "
                " src_inode, dst_existed, prior_occupant_ref, temp_path, "
                " expected_postcondition, idempotency_token) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), operation_uuid, KIND_INTENT, _iso(now),
                 job_id, operation, method, src, dst, src_size, src_mtime,
                 src_inode, dst_existed, prior_occupant_ref, temp_path,
                 expected_postcondition, idempotency_token),
            )
            # Committed BEFORE the filesystem is touched. An uncommitted intent
            # would vanish on the very crash it exists to record.
            self.conn.commit()
        except Exception as exc:
            raise LedgerWriteError(
                f"could not record intent for {operation} {src} -> {dst}: {exc}"
            ) from exc
        return operation_uuid

    # ── outcome ─────────────────────────────────────────────────────────
    def record_success(self, operation_uuid: str, *, method_used=None,
                       bytes_written=None, duration_ms=None, now=None) -> None:
        self._record_outcome(operation_uuid, succeeded=1, cause=None,
                             disk_outcome=None, method=method_used,
                             bytes_written=bytes_written,
                             duration_ms=duration_ms, detail=None, now=now)

    def record_failure(self, operation_uuid: str, verdict: FailureVerdict, *,
                       bytes_written=None, duration_ms=None, now=None) -> None:
        """Record a classified failure, storing the step-1 verdict verbatim so
        the bucket written is the one the safety rules were evaluated against."""
        self._record_outcome(
            operation_uuid, succeeded=0, cause=verdict.cause.value,
            disk_outcome=verdict.disk_outcome.value, method=None,
            bytes_written=bytes_written, duration_ms=duration_ms,
            detail=verdict.detail, now=now)

    def _record_outcome(self, operation_uuid, *, succeeded, cause, disk_outcome,
                        method, bytes_written, duration_ms, detail, now) -> None:
        try:
            cur = self.conn.execute(
                "INSERT INTO fileop_events "
                "(event_uuid, operation_uuid, kind, recorded_at, job_id, "
                " operation, method, src_path, dst_path, succeeded, cause, "
                " disk_outcome, bytes_written, duration_ms, detail) "
                "SELECT ?, ?, ?, ?, job_id, operation, COALESCE(?, method), "
                "       src_path, dst_path, ?, ?, ?, ?, ?, ? "
                "FROM fileop_events WHERE operation_uuid=? AND kind=? LIMIT 1",
                (str(uuid.uuid4()), operation_uuid, KIND_OUTCOME, _iso(now),
                 method, succeeded, cause, disk_outcome, bytes_written,
                 duration_ms, detail, operation_uuid, KIND_INTENT),
            )
        except sqlite3.IntegrityError as exc:
            # The one-outcome-per-operation index fired: a second terminal
            # outcome is a contradiction, not an update.
            self.conn.rollback()
            raise LedgerWriteError(
                f"an outcome already exists for operation {operation_uuid}"
            ) from exc
        # THE SILENT-FAILURE FIX. INSERT...SELECT over a non-existent intent
        # inserts zero rows and commits happily. Rev 1 returned normally from
        # that, so the module built to guarantee bookkeeping could record
        # nothing at all and report success.
        if cur.rowcount != 1:
            self.conn.rollback()
            raise LedgerWriteError(
                f"no outcome recorded for operation {operation_uuid}: "
                f"{cur.rowcount} row(s) written — the intent does not exist")
        self.conn.commit()

    # ── recovery ────────────────────────────────────────────────────────
    def interrupted_operations(self) -> list:
        """Intents with no outcome — operations that were in flight and never
        reported back. The only places disk and records can silently disagree."""
        rows = self.conn.execute(
            "SELECT i.operation_uuid, i.recorded_at, i.job_id, i.operation, "
            "       i.method, i.src_path, i.dst_path, i.src_size, i.src_mtime, "
            "       i.src_inode, i.dst_existed, i.prior_occupant_ref, "
            "       i.temp_path, i.expected_postcondition "
            "FROM fileop_events i "
            "WHERE i.kind=? AND NOT EXISTS ("
            "  SELECT 1 FROM fileop_events o "
            "  WHERE o.operation_uuid = i.operation_uuid AND o.kind=?) "
            "ORDER BY i.recorded_at",
            (KIND_INTENT, KIND_OUTCOME),
        ).fetchall()
        return [InterruptedOperation(*row) for row in rows]

    def reconcile_interrupted(self):
        """Classify every interrupted operation against the filesystem NOW.

        Returns ``(InterruptedOperation, DiskObservation)`` pairs. This is the
        point of the evidence snapshot: each observation can be handed to
        ``classify_failure`` and yields a real disk state — duplicate, partial,
        catastrophic — instead of an undifferentiated "something was in flight".
        """
        return [(op, op.observe_now()) for op in self.interrupted_operations()]

    def outcome_counts(self) -> dict:
        """Failure counts per bucket. Interrupted operations are their own
        bucket rather than omitted, so totals reconcile against intents."""
        counts = {}
        for disk_outcome, n in self.conn.execute(
            "SELECT COALESCE(disk_outcome,'succeeded'), COUNT(*) "
            "FROM fileop_events WHERE kind=? GROUP BY 1", (KIND_OUTCOME,)
        ):
            counts[disk_outcome] = n
        interrupted = len(self.interrupted_operations())
        if interrupted:
            counts["interrupted"] = interrupted
        return counts

    def history_for_job(self, job_id: int) -> list:
        return self.conn.execute(
            "SELECT kind, recorded_at, operation, method, src_path, dst_path, "
            "       succeeded, cause, disk_outcome, detail "
            "FROM fileop_events WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()


def _iso(now: Optional[dt.datetime]) -> str:
    return (now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)).isoformat()
