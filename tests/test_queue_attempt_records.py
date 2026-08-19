"""Durable attempt history: what the 48-hour gap had no way to record.

On 2026-08-13 three batches parked and 62 items sat for two days. After a
container restart the logs were gone, and the durable rows said only
`waiting_source` -- which is identical whether the source was attempted
repeatedly and refused every time, or never attempted at all. Those need
different diagnoses, so the incident is permanently unresolved.

The design review named the missing authority: an append-only record of
ATTEMPTS, not just current state.

It also corrected the evidence I had presented. `_pause_for_source()` rewrites
every same-source sibling in the batch with
`last_reason_code='source_temporarily_blocked'` and `transport_attempted=0`, so
counting reason codes counts POLICY CONSEQUENCES, not observations. Of 62 such
rows in that incident, exactly ONE had `transport_attempted=1`. Any
source-health decision that consumes the other 61 concludes a source is
refusing when it was asked once.
"""
import uuid

import pytest

from backend.database import DatabaseManager


@pytest.fixture
def db():
    dm = DatabaseManager()
    dm._mutate("DELETE FROM download_queue_attempts WHERE source LIKE 'test-%'",
               (), label="test_clear")
    yield dm
    dm._mutate("DELETE FROM download_queue_attempts WHERE source LIKE 'test-%'",
               (), label="test_clear")
    dm.close()


def _open(db, source="test-src", item=None):
    aid = str(uuid.uuid4())
    assert db.begin_queue_attempt(aid, item or str(uuid.uuid4()), "batch-1", source)
    return aid


class TestTheQuestionTheGapCouldNotAnswer:
    def test_attempted_and_failed_is_DISTINGUISHABLE_from_never_attempted(self, db):
        """The whole point. Two windows that look identical in current state
        must be told apart from durable history alone."""
        # window A: attempted three times, every one refused
        for _ in range(3):
            aid = _open(db, "test-attempted")
            db.close_queue_attempt(aid, "FAILED", reason_code="source_temporarily_blocked",
                                   transport_attempted=True)
        # window B: nothing was ever attempted -- no rows at all
        a = db.queue_source_observations("test-attempted")
        b = db.queue_source_observations("test-never")

        assert a["attempted"] == 3 and a["failed"] == 3
        assert b["attempted"] == 0 and b["last_attempt_at"] is None
        assert a != b, "the two histories must not be indistinguishable"

    def test_a_blocked_worker_leaves_an_OPEN_attempt(self, db):
        """Started and never closed -- the state no current-state row reveals."""
        aid = _open(db, "test-blocked")
        db._mutate("UPDATE download_queue_attempts SET started_at = "
                   "datetime('now','-3 hours') WHERE attempt_id = ?", (aid,),
                   label="test_age")

        stale = db.stale_queue_attempts(older_than_seconds=1800)

        assert any(r["attempt_id"] == aid for r in stale)

    def test_a_closed_attempt_is_never_stale(self, db):
        """Control: without this, every completed attempt would alarm."""
        aid = _open(db, "test-closed")
        db._mutate("UPDATE download_queue_attempts SET started_at = "
                   "datetime('now','-3 hours') WHERE attempt_id = ?", (aid,),
                   label="test_age")
        db.close_queue_attempt(aid, "SUCCESS", transport_attempted=True,
                               source_progress=True)

        assert not any(r["attempt_id"] == aid
                       for r in db.stale_queue_attempts(older_than_seconds=1800))


class TestObservationsVsPolicyDeferrals:
    def test_policy_deferrals_are_NOT_counted_as_source_failures(self, db):
        """The correction that mattered most: 61 of 62 rows were synthetic."""
        real = _open(db, "test-mix")
        db.close_queue_attempt(real, "FAILED", reason_code="interactive_challenge",
                               transport_attempted=True)
        for _ in range(20):                      # siblings parked by policy
            sid = _open(db, "test-mix")
            db.close_queue_attempt(sid, "INTENTIONALLY_SKIPPED",
                                   reason_code="source_temporarily_blocked",
                                   transport_attempted=False)

        obs = db.queue_source_observations("test-mix")

        assert obs["attempted"] == 1, "only the request that was actually made counts"
        assert obs["failed"] == 1

    def test_source_progress_is_the_liveness_signal(self, db):
        aid = _open(db, "test-prog")
        db.close_queue_attempt(aid, "SUCCESS", transport_attempted=True,
                               source_progress=True)
        obs = db.queue_source_observations("test-prog")
        assert obs["progressed"] == 1 and obs["last_progress_at"] is not None


class TestTerminalStateDiscipline:
    def test_an_unknown_terminal_status_is_REFUSED(self, db):
        """Silence is never success, and neither is an invented status."""
        aid = _open(db, "test-bad")
        assert db.close_queue_attempt(aid, "probably_fine") is False
        # Check the row directly. stale_queue_attempts(0) cannot see it: the
        # cutoff is `started_at < now`, and an attempt opened in the same second
        # is not strictly older -- a boundary in the TEST, not in the code.
        row = db._query_dicts(
            "SELECT terminal_status FROM download_queue_attempts WHERE attempt_id = ?",
            (aid,), default=[])
        assert row and row[0]["terminal_status"] == "IN_PROGRESS", \
            "a refused close must leave the attempt open, not silently succeed"

    def test_the_backstop_cannot_overwrite_a_real_outcome(self, db):
        """_execute closes with the real result, then its finally runs anyway.

        Without only_if_open every successful attempt would be rewritten to
        FAILED by its own cleanup -- a bug that would have made the whole
        record useless and looked like a catastrophic failure rate.
        """
        aid = _open(db, "test-backstop")
        db.close_queue_attempt(aid, "SUCCESS", transport_attempted=True,
                               source_progress=True)

        db.close_queue_attempt(aid, "FAILED", reason_code="attempt_not_closed",
                               transport_attempted=False, only_if_open=True)

        obs = db.queue_source_observations("test-backstop")
        assert obs["failed"] == 0 and obs["progressed"] == 1

    def test_the_backstop_DOES_close_a_genuinely_open_attempt(self, db):
        """Control: the guard must not disable the backstop entirely."""
        aid = _open(db, "test-backstop2")
        db.close_queue_attempt(aid, "FAILED", reason_code="attempt_not_closed",
                               transport_attempted=False, only_if_open=True)
        row = db._query_dicts(
            "SELECT terminal_status FROM download_queue_attempts WHERE attempt_id = ?",
            (aid,), default=[])
        assert row and row[0]["terminal_status"] == "FAILED"
