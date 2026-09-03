"""Three stall conditions, because one timer cannot separate two histories.

The 2026-08-13 incident sat for 48 hours with no signal. The obvious alert --
"no completion in N hours" -- would have fired, but it cannot distinguish:

    nothing was attempted            (a scheduler/ownership fault)
    everything attempted failed      (a source fault)
    a person must act                (a verification hold)

Those want different responses, and conflating them is how an operator ends up
hunting a worker bug when the truth is that someone has to complete a
challenge. So the report separates them, and these tests pin the separation --
especially that a verification hold is NEVER reported as a scheduler stall.

Completion is deliberately not the progress signal: a queue can make real
source progress without an item completing, and an item can complete without a
new source reveal.
"""
import uuid

import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db():
    dm = DatabaseManager()

    def clear():
        for t in ("download_queue_attempts", "download_queue_items",
                  "download_queue_batches"):
            dm._mutate("DELETE FROM %s" % t, (), label="test_clear")
    clear()
    yield dm
    clear()
    dm.close()


def _batch(db, uuid_, *, interval=600, auto_resume=1, hold=None,
           batch_state="scheduled"):
    db._mutate(
        "INSERT INTO download_queue_batches (batch_uuid, mode, interval_seconds, "
        "state, source, total_items, auto_resume_after_cooldown, "
        "verification_hold_source, created_at, updated_at) "
        "VALUES (?, 'staggered', ?, ?, 'hdencode', 1, ?, ?, "
        "datetime('now'), datetime('now'))",
        (uuid_, interval, batch_state, auto_resume, hold), label="test_batch")


def _item(db, batch, *, state="ready", due="-1 hour", source="hdencode"):
    iid = str(uuid.uuid4())
    ok = db._mutate(
        "INSERT INTO download_queue_items (item_uuid, batch_uuid, sequence_number, "
        "source, canonical_url, title, service_type, queue_reason, state, "
        "scheduled_for, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, 'T', 'Rapidgator', 'user_batch', "
        "?, datetime('now', ?), datetime('now'), datetime('now'))",
        (iid, batch, source, "http://x/" + iid, state, due), label="test_item")
    # A silently-failed insert would make every assertion below pass VACUOUSLY
    # on an empty queue -- which is exactly what happened before queue_reason
    # (NOT NULL) was supplied.
    assert ok, "fixture insert failed; the test would prove nothing"
    return iid


def _attempt(db, item, batch, *, progress=False, started="-1 minute"):
    aid = str(uuid.uuid4())
    db.begin_queue_attempt(aid, item, batch, "hdencode")
    db._mutate("UPDATE download_queue_attempts SET started_at = datetime('now', ?) "
               "WHERE attempt_id = ?", (started, aid), label="test_age")
    db.close_queue_attempt(aid, "SUCCESS" if progress else "FAILED",
                           transport_attempted=True, source_progress=progress)
    return aid


class TestExecutorStarvation:
    def test_due_work_with_NO_attempt_is_starvation(self, db):
        _batch(db, "b1")
        _item(db, "b1", due="-2 hours")
        r = db.queue_stall_report()
        assert r["executor_starved"] is True
        assert r["evidence"]["due_now"] == 1

    def test_due_work_WITH_a_recent_attempt_is_not(self, db):
        """Control: a working queue must not alarm."""
        _batch(db, "b1")
        i = _item(db, "b1", due="-2 hours")
        _attempt(db, i, "b1", progress=True)
        assert db.queue_stall_report()["executor_starved"] is False

    def test_work_not_yet_due_is_not_starvation(self, db):
        """Control: an item legitimately waiting its turn is not a fault."""
        _batch(db, "b1")
        _item(db, "b1", due="+1 hour")
        r = db.queue_stall_report()
        assert r["executor_starved"] is False and r["evidence"]["due_now"] == 0

    def test_a_VERIFICATION_HOLD_is_not_reported_as_starvation(self, db):
        """The mislabel that would send someone after the wrong bug.

        Not attempting is CORRECT under a hold. It is human_required, and it
        must never read as a scheduler fault.
        """
        _batch(db, "b1", hold="hdencode")
        _item(db, "b1", due="-2 hours")

        r = db.queue_stall_report()

        assert r["executor_starved"] is False, "a hold is not a scheduler stall"
        assert r["human_required"] is True


class TestSourceNoProgress:
    def test_attempts_with_no_delivery_for_too_long(self, db):
        """CORRECTED 2026-08-16 (peer review round 2). This used to assert that
        a SINGLE attempt five hours ago was enough, which encoded the defect:
        the report asked only whether some attempt existed EVER, then compared
        COALESCE(last_progress, '1970-01-01') to the deadline. With no delivery
        ever recorded the epoch fallback is older than every deadline, so the
        very first failed attempt declared the source dead -- and a stale
        attempt made a CURRENT scheduler stall read as a source fault too, so
        executor_starved and source_no_progress both fired at once. Those two
        are the entire reason this report exists.

        The claim this test is really about is "attempts are happening and
        nothing comes back", so the attempts now span the window.
        """
        _batch(db, "b1")
        i = _item(db, "b1")
        _attempt(db, i, "b1", progress=False, started="-5 hours")
        _attempt(db, i, "b1", progress=False, started="-2 hours")
        _attempt(db, i, "b1", progress=False, started="-3 minutes")
        assert db.queue_stall_report()["source_no_progress"] is True

    def test_one_old_attempt_alone_is_a_SCHEDULER_fault(self, db):
        """The other half of the correction, and the distinction the whole
        report is for: we have not ASKED the source in five hours, so any
        verdict about the source is unfounded. Nothing is being attempted --
        that is starvation."""
        _batch(db, "b1")
        i = _item(db, "b1")
        _attempt(db, i, "b1", progress=False, started="-5 hours")
        r = db.queue_stall_report()
        assert r["source_no_progress"] is False
        assert r["executor_starved"] is True, (
            "work is due and nothing is being attempted: %s" % r)

    def test_a_first_failure_does_not_condemn_the_source(self, db):
        """One fresh failure with no history is a normal first try."""
        _batch(db, "b1")
        i = _item(db, "b1")
        _attempt(db, i, "b1", progress=False, started="-1 minute")
        assert db.queue_stall_report()["source_no_progress"] is False

    def test_recent_source_progress_clears_it(self, db):
        """Control: a source that is delivering must not alarm."""
        _batch(db, "b1")
        i = _item(db, "b1")
        _attempt(db, i, "b1", progress=False, started="-5 hours")
        _attempt(db, i, "b1", progress=True, started="-2 minutes")
        assert db.queue_stall_report()["source_no_progress"] is False

    def test_the_deadline_scales_with_the_pacing(self, db):
        """A slower deliberate pace must not read as a faster failure."""
        _batch(db, "b1", interval=3600)
        _item(db, "b1")
        r = db.queue_stall_report()
        assert r["evidence"]["progress_deadline_seconds"] == 3600 * 6

    def test_no_attempts_at_all_is_starvation_not_source_failure(self, db):
        """The distinction the single-timer alert cannot make."""
        _batch(db, "b1")
        _item(db, "b1", due="-2 hours")
        r = db.queue_stall_report()
        assert r["executor_starved"] is True
        assert r["source_no_progress"] is False, \
            "with zero attempts the source has not been shown to be at fault"


class TestHumanRequired:
    def test_deferred_work_with_auto_resume_OFF_needs_a_human(self, db):
        _batch(db, "b1", auto_resume=0)
        _item(db, "b1", state="waiting_source")
        assert db.queue_stall_report()["human_required"] is True

    def test_a_healthy_queue_needs_nobody(self, db):
        """Control: without this, a report that always alarms would pass."""
        _batch(db, "b1")
        i = _item(db, "b1", due="+1 hour")
        _attempt(db, i, "b1", progress=True)
        r = db.queue_stall_report()
        assert not any((r["executor_starved"], r["source_no_progress"],
                        r["human_required"]))


class TestHoldScope:
    """DLQ-1. A verification hold is SOURCE-scoped and LIVE-batch-scoped.

    queue_stall_report used to count ANY batch row with
    verification_hold_source set, with no filter on the batch's own state
    and no match against the source of the work being evaluated. That let a
    hold on a batch nobody cares about any more (cancelled, or already
    completed) blanket-suppress every stall signal for every source, hiding a
    real, unrelated starvation.
    """

    def test_a_hold_on_a_CANCELLED_batch_does_not_suppress_an_unrelated_stall(
            self, db):
        """Repro (a): batch A held hdencode, then A was cancelled. The hold
        column is still set (cancel_batch never cleared it at 0a2751d) but A
        is no longer live, so it must not gate a due item with zero attempts
        in an unrelated batch."""
        _batch(db, "a", hold="hdencode", batch_state="cancelled")
        _batch(db, "b")
        _item(db, "b", due="-2 hours")

        r = db.queue_stall_report()

        assert r["executor_starved"] is True, (
            "a cancelled batch's stale hold column must not suppress "
            "starvation of unrelated live work: %s" % r)

    def test_a_hold_on_a_COMPLETED_batch_does_not_suppress_an_unrelated_stall(
            self, db):
        """The completed-batch variant: cancel_item() on a held batch's last
        item moves the batch to state='completed' with the hold still set."""
        _batch(db, "a", hold="hdencode", batch_state="completed")
        _batch(db, "b")
        _item(db, "b", due="-2 hours")

        r = db.queue_stall_report()

        assert r["executor_starved"] is True, (
            "a completed batch's stale hold column must not suppress "
            "starvation of unrelated live work: %s" % r)

    def test_a_LIVE_hold_does_not_suppress_a_DIFFERENT_sources_stall(self, db):
        """Repro (b): a live hdencode hold must not blanket-suppress a
        starved batch whose due items are a different source ('other')."""
        _batch(db, "a", hold="hdencode", batch_state="scheduled")
        _item(db, "a", source="hdencode", state="verification_required",
              due="-2 hours")
        _batch(db, "b")
        _item(db, "b", source="other", due="-2 hours")

        r = db.queue_stall_report()

        assert r["executor_starved"] is True, (
            "a live hold on one source must not suppress starvation of a "
            "different source's due work: %s" % r)

    def test_a_LIVE_hold_STILL_suppresses_its_OWN_source(self, db):
        """The control the fix must not break: this is the entire reason the
        suppression exists."""
        _batch(db, "a", hold="hdencode", batch_state="scheduled")
        _item(db, "a", source="hdencode", due="-2 hours")

        r = db.queue_stall_report()

        assert r["executor_starved"] is False, "a live hold is not a scheduler stall"
        assert r["human_required"] is True
