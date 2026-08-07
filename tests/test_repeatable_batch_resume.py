"""A batch may retry more than once, and a retry that delivered is not "spent".

WHAT PRODUCTION SHOWED, 2026-08-07. The single automatic resume fired at 03:25Z and
WORKED: 24 of 69 stranded grabs completed over the next 50 minutes — the first fully
automatic recovery from a source throttle this system had managed. Then at 04:07Z
HDEncode throttled again, four batches re-paused, and because `auto_resume_used` had
reached 1 they could never resume themselves again. 44 items sat parked behind a
spent retry.

So the one-shot limit, not the cooldown length, was the binding constraint. And the
resume it spent had delivered 24 items — calling that a failed attempt is simply
wrong.

THE RULE. A batch may make up to N *consecutive fruitless* automatic resumes
(`download_queue_auto_resume_max_attempts`, default 3, clamped 1..10). A resume that
completed anything since the previous automatic resume REFUNDS the budget. So the
budget only runs down on retries that achieved nothing, which is the only case where
giving up is correct.

WHY THIS IS NOT AN UNBOUNDED RETRY LOOP — the thing to be suspicious of, since
hammering a rate-limiting source is what caused the incident:

  * fruitless retries are capped, and the cap cannot exceed 10;
  * every retry waits for the coordinator's cooldown, which escalates 1h -> 2h -> 4h
    across consecutive stalls, so repeated attempts get further apart;
  * only progress refunds the budget, and progress means items actually delivered.

A batch that keeps making partial progress can retry indefinitely. That is intended
and `test_progress_can_extend_retries_indefinitely` pins it, so it is a documented
decision rather than an accident.
"""
import logging

from unittest.mock import MagicMock

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService

EXPIRED = "2000-01-01T00:00:00+00:00"
STAMP = "2026-08-07T04:00:00+00:00"


def _items(n):
    return [{"url": f"https://hdencode.org/release-{i}-2160p/",
             "title": f"Release {i}", "media_type": "movie"} for i in range(n)]


def _rig(db, *, config=None, count=4):
    fake = MagicMock()
    service = DownloadQueueService(config or {}, db, fake)
    service._coordinator_snapshot = MagicMock(return_value={"blocked": False})
    batch = service.schedule_batch(_items(count), interval_minutes=0,
                                   mode="immediate",
                                   auto_resume_after_cooldown=True)
    return service, batch["batch_uuid"]


def _pause(db, service, batch_uuid):
    """Park the batch exactly as a source-wide throttle parks one."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE download_queue_items "
            "SET state='waiting_source', queue_reason='source_deferred', "
            "    cooldown_until=?, last_reason_code='source_temporarily_blocked', "
            "    updated_at=? WHERE batch_uuid=? AND state NOT IN "
            "    ('completed','cancelled')",
            (EXPIRED, STAMP, batch_uuid))
        conn.execute(
            "UPDATE download_queue_batches SET state='paused_source', "
            "  paused_at=?, cooldown_until=?, "
            "  last_reason_code='reveal_verification_stalled' "
            "WHERE batch_uuid=?", (STAMP, EXPIRED, batch_uuid))
        service._refresh_batch_locked(conn, batch_uuid, STAMP)


def _complete_one(db, batch_uuid):
    """Simulate a delivery, which is what makes a resume 'fruitful'."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT item_uuid FROM download_queue_items WHERE batch_uuid=? "
            "AND state != 'completed' LIMIT 1", (batch_uuid,)).fetchone()
        assert row is not None, "no item left to complete; widen the fixture"
        conn.execute(
            "UPDATE download_queue_items SET state='completed', updated_at=? "
            "WHERE item_uuid=?", (STAMP, row["item_uuid"]))


def _state(service, batch_uuid):
    b = service.get_batch(batch_uuid)
    return b["state"], b["auto_resume_used"]


class TestTheBudgetReplacesTheSingleShot:

    def test_a_second_fruitless_retry_is_allowed(self, tmp_path):
        """THE PRODUCTION FAILURE. Under the old rule this second resume never
        happened and 44 items stayed parked."""
        db = DatabaseManager(str(tmp_path / "budget.db"))
        try:
            service, uuid = _rig(db)
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            state, used = _state(service, uuid)
            assert state != "paused_source" and used == 1

            _pause(db, service, uuid)          # the source shuts again
            service._maybe_auto_resume()
            state, used = _state(service, uuid)
            assert state != "paused_source", (
                "the batch must be able to try again; the single shot is what "
                "stranded 44 items in production")
            assert used == 2
        finally:
            db.close()

    def test_the_budget_is_finite(self, tmp_path):
        """Retrying forever against a source that is rate-limiting us is how the
        original incident started."""
        db = DatabaseManager(str(tmp_path / "finite.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 2})
            for expected in (1, 2):
                _pause(db, service, uuid)
                service._maybe_auto_resume()
                assert _state(service, uuid)[1] == expected

            _pause(db, service, uuid)
            service._maybe_auto_resume()
            state, used = _state(service, uuid)
            assert state == "paused_source", (
                "a batch that has burned its whole budget without delivering "
                "anything must stop and wait for a human")
            assert used == 2, "no further attempt may be recorded"
        finally:
            db.close()

    def test_one_attempt_restores_the_old_behaviour(self, tmp_path):
        """An escape hatch: the previous policy is still reachable by config."""
        db = DatabaseManager(str(tmp_path / "single.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 1})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 1
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[0] == "paused_source"
        finally:
            db.close()


class TestProgressRefundsTheBudget:

    def test_a_resume_that_delivered_does_not_spend_the_budget(self, tmp_path,
                                                               caplog):
        """THE 24-ITEM CASE. A retry that worked is not a failed attempt."""
        db = DatabaseManager(str(tmp_path / "refund.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 2})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 1

            _complete_one(db, uuid)            # the resume delivered something
            _pause(db, service, uuid)
            with caplog.at_level(logging.INFO):
                service._maybe_auto_resume()

            state, used = _state(service, uuid)
            assert state != "paused_source"
            assert used == 1, (
                "delivering an item must restore the budget, so this counts as "
                "the FIRST fruitless attempt rather than the second")
            assert any("restoring its retry budget" in r.getMessage()
                       for r in caplog.records), (
                "the refund must be visible in the log; a silent budget change "
                "is impossible to reason about after the fact")
        finally:
            db.close()

    def test_progress_can_extend_retries_indefinitely(self, tmp_path):
        """PINNED DECISION, not an oversight. A batch that keeps delivering keeps
        earning retries. It is still spaced by the escalating cooldown."""
        db = DatabaseManager(str(tmp_path / "forever.db"))
        try:
            service, uuid = _rig(db, count=8, config={
                "download_queue_auto_resume_max_attempts": 1})
            for _ in range(4):
                _pause(db, service, uuid)
                service._maybe_auto_resume()
                assert _state(service, uuid)[0] != "paused_source", (
                    "each cycle delivered an item, so each resume must be "
                    "permitted even with a budget of 1")
                _complete_one(db, uuid)
        finally:
            db.close()

    def test_no_progress_means_no_refund(self, tmp_path):
        """The negative control for the refund: without a delivery the budget
        must actually run down, or the cap is decorative."""
        db = DatabaseManager(str(tmp_path / "norefund.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 3})
            for expected in (1, 2, 3):
                _pause(db, service, uuid)
                service._maybe_auto_resume()
                assert _state(service, uuid)[1] == expected, (
                    "with nothing delivered the counter must climb every time")
        finally:
            db.close()

    def test_a_manual_resume_never_spends_the_budget(self, tmp_path):
        """Operator action is not an automatic attempt."""
        db = DatabaseManager(str(tmp_path / "manual.db"))
        try:
            service, uuid = _rig(db)
            _pause(db, service, uuid)
            service._resume_batch(uuid, interval_minutes=0, automated=False)
            assert _state(service, uuid)[1] == 0
        finally:
            db.close()


class TestTheConfigIsSane:

    def test_bad_values_fall_back_and_are_clamped(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "cfg.db"))
        try:
            for value, expected in (("nonsense", 3), (None, 3), (0, 1),
                                    (-5, 1), (999, 10), (2, 2)):
                # No batch scheduled here on purpose: this is about config
                # parsing, and scheduling the same URLs twice in one database
                # raises DownloadQueueConflict, which would fail the test for a
                # reason unrelated to what it is checking.
                service = DownloadQueueService(
                    {"download_queue_auto_resume_max_attempts": value},
                    db, MagicMock())
                assert service._auto_resume_max_attempts() == expected, value
        finally:
            db.close()

    def test_the_progress_mark_column_exists(self, tmp_path):
        """The migration is placed after the CREATE, not in the shared list that
        runs before it -- an ALTER there fails with 'no such table' and the guard
        only swallows 'duplicate column', so the column would be silently absent."""
        db = DatabaseManager(str(tmp_path / "schema.db"))
        try:
            cols = [r["name"] for r in db._query_dicts(
                "PRAGMA table_info(download_queue_batches)", default=[])]
            assert "auto_resume_progress_mark" in cols, cols
        finally:
            db.close()
