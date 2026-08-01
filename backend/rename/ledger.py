"""Append-only file-operation ledger — safety-gate step 2.

`rename_jobs` records the CURRENT STATE of a job. That is a different thing from
a history, and the difference is exactly the gap this closes: when a process
dies mid-move, the job row simply sits in whatever status it last held. Nothing
anywhere says "a file was being moved from A to B at that moment", so the one
window where disk and database can silently disagree leaves no trace at all.

Two rules make this ledger able to answer that:

1. **INTENT IS RECORDED BEFORE THE FILESYSTEM IS TOUCHED, AND THE WRITE MUST
   SUCCEED.** If the intent cannot be persisted, the operation does not run.
   This is deliberately not best-effort — SH-R03 already taught this codebase
   what "best-effort" bookkeeping costs: the trash manifest's write was wrapped
   in `except OSError: logger.warning(...)`, but `restore_trash_entry` HARD
   REFUSES any entry without a manifest record, so a "degraded" write was in
   fact permanent, unrecoverable loss.

2. **ROWS ARE NEVER UPDATED.** An outcome is a SECOND row referencing the
   intent's uuid. A bug in the outcome path therefore cannot destroy the record
   of intent — which is the record that matters when something has gone wrong.

The recovery signal falls straight out: an `intent` row with no matching
`outcome` row IS an interrupted operation. `interrupted_operations()` returns
them, and they are precisely the paths a human needs to look at before anything
else touches them.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional

from backend.rename.failure import Cause, DiskOutcome, FailureVerdict

KIND_INTENT = "intent"
KIND_OUTCOME = "outcome"


class LedgerWriteError(RuntimeError):
    """The intent could not be persisted, so the operation must not proceed."""


@dataclass(frozen=True)
class InterruptedOperation:
    event_uuid: str
    recorded_at: str
    job_id: Optional[int]
    operation: str
    method: Optional[str]
    src_path: Optional[str]
    dst_path: Optional[str]

    @property
    def summary(self) -> str:
        return (f"{self.operation}({self.method or '?'}) "
                f"{self.src_path} -> {self.dst_path} "
                f"began {self.recorded_at} and never reported an outcome")


class FileOpLedger:
    """Append-only record of what the app was about to do, and what happened."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── intent ──────────────────────────────────────────────────────────
    def record_intent(self, *, operation: str, src: Optional[str] = None,
                      dst: Optional[str] = None, method: Optional[str] = None,
                      job_id: Optional[int] = None,
                      now: Optional[dt.datetime] = None) -> str:
        """Persist the intent and return its uuid. Raises if it cannot.

        The caller MUST treat an exception here as "do not perform the file
        operation". An unrecorded operation is one that cannot be recovered
        from, and refusing to start is always cheaper than that.
        """
        event_uuid = str(uuid.uuid4())
        try:
            self.conn.execute(
                "INSERT INTO fileop_events "
                "(event_uuid, kind, recorded_at, job_id, operation, method, "
                " src_path, dst_path) VALUES (?,?,?,?,?,?,?,?)",
                (event_uuid, KIND_INTENT, _iso(now), job_id, operation, method,
                 src, dst),
            )
            # Committed BEFORE the filesystem is touched. An uncommitted intent
            # would vanish on the very crash it exists to record.
            self.conn.commit()
        except Exception as exc:
            raise LedgerWriteError(
                f"could not record intent for {operation} {src} -> {dst}: {exc}"
            ) from exc
        return event_uuid

    # ── outcome ─────────────────────────────────────────────────────────
    def record_success(self, event_uuid: str, *, method_used: Optional[str] = None,
                       bytes_written: Optional[int] = None,
                       duration_ms: Optional[int] = None,
                       now: Optional[dt.datetime] = None) -> None:
        self._record_outcome(event_uuid, succeeded=1, cause=None,
                             disk_outcome=None, method=method_used,
                             bytes_written=bytes_written, duration_ms=duration_ms,
                             detail=None, now=now)

    def record_failure(self, event_uuid: str, verdict: FailureVerdict, *,
                       bytes_written: Optional[int] = None,
                       duration_ms: Optional[int] = None,
                       now: Optional[dt.datetime] = None) -> None:
        """Record a classified failure. Takes the step-1 verdict directly so the
        bucket stored is the one the safety rules were evaluated against, not a
        re-derivation that could drift from it."""
        self._record_outcome(
            event_uuid, succeeded=0, cause=verdict.cause.value,
            disk_outcome=verdict.disk_outcome.value, method=None,
            bytes_written=bytes_written, duration_ms=duration_ms,
            detail=verdict.detail, now=now)

    def _record_outcome(self, event_uuid, *, succeeded, cause, disk_outcome,
                        method, bytes_written, duration_ms, detail, now) -> None:
        # Unlike the intent, an outcome write that fails is not fatal: the
        # intent row survives, so the operation simply reads as interrupted —
        # which is the safe reading, not a lost one.
        self.conn.execute(
            "INSERT INTO fileop_events "
            "(event_uuid, kind, recorded_at, job_id, operation, method, "
            " src_path, dst_path, succeeded, cause, disk_outcome, "
            " bytes_written, duration_ms, detail) "
            "SELECT ?, ?, ?, job_id, operation, COALESCE(?, method), "
            "       src_path, dst_path, ?, ?, ?, ?, ?, ? "
            "FROM fileop_events WHERE event_uuid=? AND kind=? LIMIT 1",
            (event_uuid, KIND_OUTCOME, _iso(now), method, succeeded, cause,
             disk_outcome, bytes_written, duration_ms, detail,
             event_uuid, KIND_INTENT),
        )
        self.conn.commit()

    # ── recovery ────────────────────────────────────────────────────────
    def interrupted_operations(self) -> list:
        """Intents with no outcome — operations that were in flight and never
        reported back. These are the only places disk and records can silently
        disagree, so they are what a human is shown first."""
        rows = self.conn.execute(
            "SELECT i.event_uuid, i.recorded_at, i.job_id, i.operation, "
            "       i.method, i.src_path, i.dst_path "
            "FROM fileop_events i "
            "WHERE i.kind=? AND NOT EXISTS ("
            "  SELECT 1 FROM fileop_events o "
            "  WHERE o.event_uuid = i.event_uuid AND o.kind=?) "
            "ORDER BY i.recorded_at",
            (KIND_INTENT, KIND_OUTCOME),
        ).fetchall()
        return [InterruptedOperation(*row) for row in rows]

    def outcome_counts(self) -> dict:
        """Failure counts per bucket — what the free-text column could never
        give us. Interrupted operations are counted as their own bucket rather
        than omitted, so the total always reconciles against intents."""
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
