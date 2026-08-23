"""The "did not auto-resume" diagnostic reports a CHANGE, not a heartbeat.

The diagnostic exists for a good reason -- see test_auto_resume_diagnostics.py,
where a batch that could never resume failed silently for a week. It must keep
firing.

What it must not do is fire on every scheduler pass. A batch holding one item
under a verification hold can NEVER auto-resume by design: only a human probe
releases it. So the same unchanging sentence was re-emitted indefinitely.

Measured on the live system 2026-08-23, shortly after the queue became active
again: 129 identical lines in five minutes from ONE batch, and 993 of 3,172
lines in the log file -- 31% of the whole log, growing at roughly 26 a minute.

The requirement pulls in two directions and both matter:
  * an unchanged reason must be reported ONCE
  * a CHANGED reason, and a reason recurring after a successful resume, must
    be reported again -- those transitions are the operator-visible events
"""
import logging

from backend.database import DatabaseManager
from tests.test_auto_resume_diagnostics import _paused_batch, EXPIRED


def _held(db):
    """A batch parked on a verification hold: the never-resumable shape."""
    return _paused_batch(
        db, item_cooldown=EXPIRED, batch_cooldown=EXPIRED,
        queue_reason="interactive_challenge",
        last_reason_code="interactive_challenge",
    )


def _warnings(caplog):
    return [r for r in caplog.records if "did not auto-resume" in r.getMessage()]


class TestAnUnchangedReasonIsReportedOnce:

    def test_ten_passes_produce_one_warning(self, tmp_path, caplog):
        db = DatabaseManager(str(tmp_path / "once.db"))
        try:
            service, _ = _held(db)
            with caplog.at_level(logging.WARNING):
                for _ in range(10):
                    service._maybe_auto_resume()
            n = len(_warnings(caplog))
            assert n == 1, (
                "ten identical evaluations produced %d warnings; the live system "
                "was emitting ~26 a minute this way" % n)
        finally:
            db.close()

    def test_the_first_pass_still_reports(self, tmp_path, caplog):
        """The positive control, and the whole point of the diagnostic.
        Suppressing the repeat must not suppress the FIRST one -- that would
        restore the silent failure this diagnostic was built to end."""
        db = DatabaseManager(str(tmp_path / "first.db"))
        try:
            service, batch_uuid = _held(db)
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            warns = _warnings(caplog)
            assert len(warns) == 1
            msg = warns[0].getMessage()
            assert batch_uuid[:8] in msg
            assert "verification hold" in msg, (
                "the warning no longer names the cause: %s" % msg)
        finally:
            db.close()


class TestATransitionIsReportedAgain:

    def test_a_changed_reason_reports_again(self, tmp_path, caplog):
        """Keyed on the cause TEXT, not just the batch. A batch whose reason
        changes is exactly what an operator needs to see."""
        db = DatabaseManager(str(tmp_path / "changed.db"))
        try:
            service, batch_uuid = _held(db)
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
                first = len(_warnings(caplog))
                # Same batch, different cause.
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE download_queue_items SET queue_reason = ?, "
                        "last_reason_code = ? WHERE batch_uuid = ?",
                        ("source_deferred", "operation_timeout_unknown", batch_uuid))
                service._maybe_auto_resume()
                second = len(_warnings(caplog))
            assert first == 1
            assert second > first, (
                "the reason changed and nothing was reported; suppression is "
                "keyed too coarsely")
        finally:
            db.close()

    def test_nothing_clears_the_suppression_mid_flight(self, tmp_path, caplog):
        """Deliberately NOT cleared, and this test pins why.

        Two earlier attempts cleared it -- once at _resume_batch entry, once on
        its success path -- and BOTH defeated the suppression completely.
        _resume_batch is attempted on every scheduler pass, and in the
        oscillating case it succeeds and the batch re-blocks immediately, so
        either clear ran every pass. Measured: 20 passes still produced 20
        warnings. Only removing the clear entirely gave 1.
        """
        db = DatabaseManager(str(tmp_path / "noclear.db"))
        try:
            service, _ = _held(db)
            with caplog.at_level(logging.WARNING):
                for _ in range(20):
                    service._maybe_auto_resume()
            assert len(_warnings(caplog)) == 1
        finally:
            db.close()

    def test_two_different_batches_each_report(self, tmp_path, caplog):
        """Suppression is per batch. One stuck batch must not silence another."""
        db = DatabaseManager(str(tmp_path / "two.db"))
        try:
            a, uuid_a = _held(db)
            # A second SERVICE on its own database: _paused_batch reuses the
            # same item URLs, and scheduling them twice in one queue is
            # correctly refused as already-active.
            db2 = DatabaseManager(str(tmp_path / "two-b.db"))
            b, uuid_b = _held(db2)
            with caplog.at_level(logging.WARNING):
                a._maybe_auto_resume()
                b._maybe_auto_resume()
            reported = {w.getMessage().split()[1][:8] for w in _warnings(caplog)}
            assert len(reported) >= 2, (
                "two distinct stuck batches produced warnings for %d of them"
                % len(reported))
        finally:
            db.close()
            try:
                db2.close()
            except Exception:
                pass
