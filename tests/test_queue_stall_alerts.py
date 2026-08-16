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


def _batch(db, uuid_, *, interval=600, auto_resume=1, hold=None):
    db._mutate(
        "INSERT INTO download_queue_batches (batch_uuid, mode, interval_seconds, "
        "state, source, total_items, auto_resume_after_cooldown, "
        "verification_hold_source, created_at, updated_at) "
        "VALUES (?, 'staggered', ?, 'scheduled', 'hdencode', 1, ?, ?, "
        "datetime('now'), datetime('now'))",
        (uuid_, interval, auto_resume, hold), label="test_batch")


def _item(db, batch, *, state="ready", due="-1 hour"):
    iid = str(uuid.uuid4())
    ok = db._mutate(
        "INSERT INTO download_queue_items (item_uuid, batch_uuid, sequence_number, "
        "source, canonical_url, title, service_type, queue_reason, state, "
        "scheduled_for, created_at, updated_at) "
        "VALUES (?, ?, 0, 'hdencode', ?, 'T', 'Rapidgator', 'user_batch', "
        "?, datetime('now', ?), datetime('now'), datetime('now'))",
        (iid, batch, "http://x/" + iid, state, due), label="test_item")
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
        _batch(db, "b1")
        i = _item(db, "b1")
        _attempt(db, i, "b1", progress=False, started="-5 hours")
        assert db.queue_stall_report()["source_no_progress"] is True

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
