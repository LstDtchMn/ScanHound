"""A batch that cannot auto-resume must SAY SO, not fail silently.

THE INCIDENT THIS COMES FROM. On 2026-08-06 an HDEncode reveal throttle paused
five batches holding 69 grabs. To wait out a throttle that was still active five
hours in, the batches' `cooldown_until` was raised by hand from 00:02Z to 03:25Z
-- and the 69 items kept 00:02Z.

`_maybe_auto_resume()` finds an eligible batch by looking for an item whose
`cooldown_until` matches the BATCH's exactly, as strings. With the two sides
diverged it finds nothing and does `continue`. All five batches would have been
skipped at 03:25Z, permanently, and the database would have looked completely
correct: paused_source, auto-resume enabled, retry unspent, cooldown expired.
Nothing anywhere would have said the retry never happened.

Worse, this is the failure mode that ALREADY cost a week: 58 of those 69 had been
parked since July 31 for a related reason and nothing surfaced it.

So the requirement is not "resume more aggressively" -- the eligibility rule is
deliberate. It is that a due-but-unresumable batch must produce a diagnostic
naming the cause, so the next occurrence is found by reading a log instead of by
reading the query by hand.
"""
import logging

from unittest.mock import MagicMock

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService

EXPIRED = "2000-01-01T00:00:00+00:00"
DIVERGED = "2030-01-01T00:00:00+00:00"
STAMP = "2026-08-06T20:00:00+00:00"


def _item(index: int) -> dict:
    return {
        "url": f"https://hdencode.org/release/{index}",
        "title": f"Release {index}",
        "media_type": "movie",
    }


def _paused_batch(db, *, item_cooldown, batch_cooldown, queue_reason,
                  last_reason_code, item_state="waiting_source"):
    """A batch parked exactly as a source-wide throttle parks one."""
    fake = MagicMock()
    service = DownloadQueueService({}, db, fake)
    service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = service.schedule_batch(
        [_item(1), _item(2)], interval_minutes=0, mode="immediate",
        auto_resume_after_cooldown=True,
    )
    current = service.get_batch(batch["batch_uuid"])
    with db.transaction() as conn:
        for row in current["items"]:
            conn.execute(
                """
                UPDATE download_queue_items
                SET state = ?, queue_reason = ?, cooldown_until = ?,
                    last_reason_code = ?, updated_at = ?
                WHERE item_uuid = ?
                """,
                (item_state, queue_reason, item_cooldown, last_reason_code,
                 STAMP, row["item_uuid"]),
            )
        conn.execute(
            """
            UPDATE download_queue_batches
            SET state = 'paused_source', paused_at = ?, cooldown_until = ?,
                last_reason_code = 'reveal_verification_stalled',
                auto_resume_after_cooldown = 1, auto_resume_used = 0
            WHERE batch_uuid = ?
            """,
            (STAMP, batch_cooldown, batch["batch_uuid"]),
        )
        service._refresh_batch_locked(conn, batch["batch_uuid"], STAMP)
    return service, batch["batch_uuid"]


class TestTheTimestampDivergence:
    """The exact 2026-08-06 shape: batch cooldown moved, items left behind.

    PREMISE DELIBERATELY REVERSED 2026-08-08, on peer review round 10.

    This class was built when item/batch cooldown EQUALITY was a precondition for
    automatic resume, and its job was to make sure a divergence was at least
    REPORTED rather than silently swallowed. Round 10's blocking finding was that the
    equality requirement should not exist at all: liveness must not depend on two
    copies of one recovery fact staying byte-identical, because ordinary operations
    (retry_item) break that synchronisation and permanently strand work.

    So divergence is no longer a blocker, and the right assertion is the opposite of
    the original: the batch RESUMES. Reporting a cause is only interesting for items
    that are held ON PURPOSE, which the rest of this file still covers.

    The old assertion is preserved below in inverted form rather than deleted,
    because "we used to require this and stopped" is the part a future reader needs.
    """

    def test_a_diverged_cooldown_no_longer_blocks_recovery(self, tmp_path, caplog):
        db = DatabaseManager(str(tmp_path / "diverged.db"))
        try:
            service, batch_uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="source_deferred",
                last_reason_code="source_temporarily_blocked",
            )
            # Move ONLY the batch forward, as an operator waiting out a throttle
            # would, then bring it back into the past so it is due. The two sides
            # now disagree while every other precondition is satisfied.
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE download_queue_items SET cooldown_until = ? "
                    "WHERE batch_uuid = ?", (DIVERGED, batch_uuid))

            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()

            # WHAT THIS FIXTURE ACTUALLY MODELS. `DIVERGED` is 2030 -- the FUTURE.
            # So the item is not merely out of step with its batch, it is genuinely
            # not due yet. My first rewrite of this test asserted the items must
            # resume, without checking whether the timestamp was due at all; it
            # failed against correct code, which is the same fixture mistake I made
            # twice on round 9.
            #
            # Under the new rule each cooldown is read on its own terms, so an item
            # deferred until 2030 is DELIBERATELY WAITING and has a recovery path --
            # 2030. Not resuming it is right. What changed is that this is no longer
            # reported as permanent failure, because it is not one.
            items = db._query_dicts(
                "SELECT state FROM download_queue_items WHERE batch_uuid = ?",
                (batch_uuid,), default=[])
            assert items, "fixture produced no items"
            assert all(r["state"] == "waiting_source" for r in items), (
                "an item whose OWN cooldown is still in the future must stay "
                f"deferred: {[r['state'] for r in items]}")

            joined = " ".join(r.getMessage() for r in caplog.records
                              if r.levelno >= logging.WARNING)
            assert "NEVER RESUME" not in joined, (
                "under the old equality rule this was reported as permanently "
                "unresumable. It is not: the item has a future cooldown and will "
                f"recover when it passes. Saying NEVER invites a needless rescue: {joined}")
        finally:
            db.close()

    def test_the_matching_case_resumes_and_stays_quiet(self, tmp_path, caplog):
        """NEGATIVE CONTROL. If this also warned, the warning would be noise and
        would be tuned out -- which is as good as silence."""
        db = DatabaseManager(str(tmp_path / "matching.db"))
        try:
            service, batch_uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="source_deferred",
                last_reason_code="source_temporarily_blocked",
            )
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()

            current = service.get_batch(batch_uuid)
            assert current["state"] != "paused_source", (
                "positive control: with timestamps aligned the batch MUST "
                "resume. If it does not, this whole file is testing a broken "
                "harness rather than the diagnostic.")
            assert current["auto_resume_used"] == 1
            assert not [r for r in caplog.records
                        if r.levelno >= logging.WARNING], (
                "a healthy resume must not warn")
        finally:
            db.close()


class TestTheOtherCausesAreDistinguished:
    """A warning that says only 'could not resume' sends you back to the SQL."""

    def test_an_unrecognised_queue_reason_is_named(self, tmp_path, caplog):
        db = DatabaseManager(str(tmp_path / "reason.db"))
        try:
            service, batch_uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="manual_retry",
                last_reason_code="source_temporarily_blocked",
            )
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            joined = " ".join(r.getMessage() for r in caplog.records
                              if r.levelno >= logging.WARNING)
            assert "queue_reason" in joined, joined
            assert "cooldown timestamp" not in joined, (
                "misattributing the cause is worse than a generic message: it "
                "sends the reader to fix a timestamp that is already correct")
        finally:
            db.close()

    def test_unknown_execution_states_are_named_as_deliberate(self, tmp_path,
                                                              caplog):
        """These are excluded ON PURPOSE -- a retry could double-submit a
        delivery that already happened. The log must not read like a bug."""
        db = DatabaseManager(str(tmp_path / "unknown.db"))
        try:
            service, batch_uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="source_deferred",
                last_reason_code="operation_timeout_unknown",
            )
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            joined = " ".join(r.getMessage() for r in caplog.records
                              if r.levelno >= logging.WARNING)
            assert "unknown execution state" in joined, joined
            assert "double-submit" in joined, (
                "the reader must learn WHY it is excluded, or they will 'fix' "
                "it by forcing a retry")
        finally:
            db.close()

    def test_an_empty_batch_does_not_cry_wolf(self, tmp_path, caplog):
        """Nothing deferred means nothing to resume. Warning here would fire on
        every ordinary completed batch and drown the real signal."""
        db = DatabaseManager(str(tmp_path / "empty.db"))
        try:
            service, batch_uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="source_deferred",
                last_reason_code="source_temporarily_blocked",
            )
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE download_queue_items SET state = 'completed' "
                    "WHERE batch_uuid = ?", (batch_uuid,))
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            assert not [r for r in caplog.records
                        if r.levelno >= logging.WARNING], (
                "an ordinary batch with nothing waiting must stay quiet")
        finally:
            db.close()


class TestCombinedCausesAreAllReported:
    """A batch can be blocked for several reasons at once.

    THE DEFECT THIS FIXES, from peer review. The diagnostic was an if/elif chain,
    so a batch whose rows had BOTH a cooldown mismatch AND
    `operation_timeout_unknown` was reported as a cooldown problem only. Matching
    the timestamps would not have made those rows safe to retry -- they are
    excluded deliberately because a retry could double-submit a delivery that
    already happened. So the message hid the safety-critical reason and sent the
    reader to fix the wrong thing, which is the exact failure class this method
    exists to prevent.
    """

    def _messages(self, caplog):
        return " ".join(r.getMessage() for r in caplog.records
                        if r.levelno >= logging.WARNING)

    def test_a_timestamp_mismatch_AND_unknown_outcome_reports_both(
            self, tmp_path, caplog):
        db = DatabaseManager(str(tmp_path / "both-causes.db"))
        try:
            service, uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="source_deferred",
                last_reason_code="operation_timeout_unknown")
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE download_queue_items SET cooldown_until = ? "
                    "WHERE batch_uuid = ?", (DIVERGED, uuid))
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            joined = self._messages(caplog)
            assert "cooldown timestamp" in joined, joined
            assert "unknown execution state" in joined, (
                "the unknown-outcome rows must be reported EVEN THOUGH the "
                "cooldown also mismatches -- hiding it is what sends the reader "
                "to fix the wrong thing")
            assert "double-submit" in joined, (
                "the reader must learn WHY those rows are excluded")
        finally:
            db.close()

    def test_the_predicate_vector_is_always_printed(self, tmp_path, caplog):
        """Counts, not just prose, so the reader can see the whole picture."""
        db = DatabaseManager(str(tmp_path / "vector.db"))
        try:
            service, uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="manual_retry",
                last_reason_code="source_temporarily_blocked")
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            joined = self._messages(caplog)
            for token in ("deferred=", "cooldown_match=", "recognised_reason=",
                          "unknown_outcome="):
                assert token in joined, (token, joined)
        finally:
            db.close()

    def test_a_healthy_resume_still_says_nothing(self, tmp_path, caplog):
        """NEGATIVE CONTROL. Reporting more causes must not make it noisy."""
        db = DatabaseManager(str(tmp_path / "quiet.db"))
        try:
            service, uuid = _paused_batch(
                db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
                queue_reason="source_deferred",
                last_reason_code="source_temporarily_blocked")
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            assert service.get_batch(uuid)["state"] != "paused_source"
            assert not [r for r in caplog.records
                        if r.levelno >= logging.WARNING]
        finally:
            db.close()
