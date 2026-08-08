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
  * every retry waits for whatever cooldown the coordinator has stored;
  * only real source progress refunds the budget -- and "real" means a delivery that
    crossed the source boundary, not any completed row.

NOT CLAIMED HERE: that the waits GROW. An earlier version of this docstring said the
coordinator escalates 1h -> 2h -> 4h so repeated attempts get further apart. Peer
review found `observe_reveal_success()` has no production call site and nothing in
this branch drives the real coordinator, so that composition is unproven. The claim
is withdrawn rather than restated.

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


def _outcome(method, *, transport=True):
    """An outcome dict shaped like the real one public_download_result emits."""
    return {"success": True, "method": method, "link_count": 1,
            "message": "x", "reason_code": "", "stage": "download",
            "retryable": False, "retry_mode": "none",
            "transport_attempted": None, "source_progress": transport,
            "affected_scope": "item",
            "action_code": "", "signals": []}


def _complete_one(db, service, batch_uuid, method="jdownloader", transport=True):
    """Complete an item through the REAL production path.

    THE FIXTURE DEFECT THIS REPLACES, named by peer review. The old helper wrote
    `state='completed'` straight into the row, so it could not tell a real source
    delivery from a pre-scrape duplicate -- which is exactly the distinction the
    refund depends on. It certified the proxy, not the property. Now it goes
    through `_complete`, so the outcome's method and transport flag decide whether
    the source counter moves, as in production.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM download_queue_items WHERE batch_uuid=? "
            "AND state NOT IN ('completed','cancelled') LIMIT 1",
            (batch_uuid,)).fetchone()
        assert row is not None, "no item left to complete; widen the fixture"
        item = dict(row)
        # _complete requires the row to be claimed BY THIS WORKER; without
        # claimed_by it logs "ignored stale completion" and changes nothing, which
        # would make these tests pass for the wrong reason.
        conn.execute("UPDATE download_queue_items SET state='claimed', "
                     "claimed_by=? WHERE item_uuid=?",
                     (service.worker_id, item["item_uuid"]))
    service._complete(item, _outcome(method, transport=transport))


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

            _complete_one(db, service, uuid)            # the resume delivered something
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
        earning retries, and each retry still waits for the cooldown the coordinator
        has stored.

        NOT ASSERTED: that those waits grow. The 1h -> 2h -> 4h composition is
        unproven -- observe_reveal_success() has no production call site and nothing
        here drives the real coordinator -- so that claim is withdrawn rather than
        restated."""
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
                _complete_one(db, service, uuid)
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


class TestOnlyRealSourceProgressRefundsTheBudget:
    """THE HIGH BLOCKER from peer review, and why the old test could not see it.

    #51 refunded the retry budget whenever the batch's count of `completed` items
    grew, and its own text claimed "progress means items actually delivered". That
    is not what `completed` means. `download_item()` deduplicates BEFORE scraping:
    if the release is already grabbed it returns success with method='duplicate'
    having contacted nothing. So a batch could earn another automatic retry against
    a throttling source purely because an item was already satisfied elsewhere.

    The old fixture wrote `state='completed'` directly, so it certified the proxy
    rather than the property. These go through the real `_complete`.
    """

    def test_a_real_delivery_refunds(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "real.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 2})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 1
            _complete_one(db, service, uuid, method="jdownloader")
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 1, (
                "a genuine source delivery must restore the budget")
        finally:
            db.close()

    def test_an_exact_duplicate_does_NOT_refund(self, tmp_path):
        """The counterexample the review supplied. Nothing reached HDEncode."""
        db = DatabaseManager(str(tmp_path / "dupe.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 2})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            _complete_one(db, service, uuid, method="duplicate", transport=False)
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 2, (
                "a pre-scrape duplicate proves nothing about the source and must "
                "not buy another retry")
        finally:
            db.close()

    def test_duplicate_similar_does_NOT_refund(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "dupesim.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 2})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            _complete_one(db, service, uuid, method="duplicate_similar",
                          transport=False)
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 2
        finally:
            db.close()

    def test_a_completion_without_transport_does_not_refund(self, tmp_path):
        """Belt and braces: even an unrecognised method must not count when the
        outcome says transport never happened, so a future short-circuit that
        forgets to name itself is still handled."""
        db = DatabaseManager(str(tmp_path / "notransport.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 2})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            _complete_one(db, service, uuid, method="some_future_shortcut",
                          transport=False)
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 2
        finally:
            db.close()

    def test_the_predicate_itself(self):
        """Direct coverage of the predicate.

        REWRITTEN 2026-08-07. This previously asserted the transport_attempted
        contract, and peer review showed that contract was unsatisfiable in
        production: no real success path sets that field, so the counter never
        incremented and the refund could never fire. The predicate now keys on the
        affirmative `source_progress` signal the producer sets where the delivery
        happens. See tests/test_source_progress_contract.py, which asserts the
        producer actually emits it -- the check whose absence let this go unnoticed.
        """
        assert DownloadQueueService.is_source_delivery({"source_progress": True})
        for bad in ({"source_progress": False},
                    {"method": "jdownloader", "transport_attempted": True},
                    {"method": "duplicate", "source_progress": False},
                    {}):
            assert not DownloadQueueService.is_source_delivery(bad), bad

    def test_a_real_success_shape_still_counts(self):
        """A production success carries transport_attempted=None. If anything
        re-introduces a requirement on that field, real deliveries silently stop
        counting again -- which is exactly what happened."""
        assert DownloadQueueService.is_source_delivery(
            {"success": True, "method": "jdownloader",
             "transport_attempted": None, "source_progress": True})


class TestMigratedBatchesGetNoFreeCredit:
    """MEDIUM from review: the additive column left every existing batch with a
    progress mark of 0, so an old batch with historical completions satisfied
    `completed > 0` and earned an extra resume even at max_attempts=1 -- making the
    claim "1 restores the previous behaviour exactly" false for migrated batches.

    Keying the refund on `source_delivery_count` fixes it by construction: counter
    and mark both start at 0, so `0 > 0` is false and no history is inferred.
    """

    def test_an_old_batch_with_completions_gets_no_extra_resume(self, tmp_path):
        db = DatabaseManager(str(tmp_path / "migrated.db"))
        try:
            service, uuid = _rig(db, count=6, config={
                "download_queue_auto_resume_max_attempts": 1})
            with db.transaction() as conn:
                for row in conn.execute(
                        "SELECT item_uuid FROM download_queue_items "
                        "WHERE batch_uuid=? LIMIT 3", (uuid,)).fetchall():
                    conn.execute(
                        "UPDATE download_queue_items SET state='completed' "
                        "WHERE item_uuid=?", (row["item_uuid"],))
                conn.execute(
                    "UPDATE download_queue_batches SET source_delivery_count=0, "
                    "auto_resume_progress_mark=0 WHERE batch_uuid=?", (uuid,))
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[1] == 1
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            assert _state(service, uuid)[0] == "paused_source", (
                "max_attempts=1 must restore the old behaviour EXACTLY, including "
                "for a migrated batch that already had completed rows")
        finally:
            db.close()


class TestAnExhaustedBudgetIsVisible:
    """MEDIUM combined-set blocker: #51 created a new terminal automatic state that
    the eligibility query filters out, so it never reaches #47's diagnostic. The
    tests said "stop and wait for a human"; nothing told the human."""

    def test_exhaustion_warns_once_naming_the_batch(self, tmp_path, caplog):
        db = DatabaseManager(str(tmp_path / "exhausted.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 1})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            _pause(db, service, uuid)
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            msgs = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
            assert any(uuid in m and "auto_resume_budget_exhausted" in m
                       for m in msgs), msgs
            assert any("will NOT retry on its own" in m for m in msgs)

            caplog.clear()
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
                service._maybe_auto_resume()
            assert not [r for r in caplog.records
                        if r.levelno >= logging.WARNING], (
                "it must warn once, not on every worker poll")
        finally:
            db.close()

    def test_a_healthy_batch_does_not_warn(self, tmp_path, caplog):
        """NEGATIVE CONTROL: a batch with budget left must stay quiet."""
        db = DatabaseManager(str(tmp_path / "healthy.db"))
        try:
            service, uuid = _rig(db, config={
                "download_queue_auto_resume_max_attempts": 3})
            _pause(db, service, uuid)
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            assert not [r for r in caplog.records
                        if r.levelno >= logging.WARNING]
        finally:
            db.close()

    def test_a_second_exhaustion_episode_warns_again(self, tmp_path, caplog):
        """PER EPISODE, not once per process.

        Peer review noted that keying the warning on the batch alone means a batch
        that recovers and later exhausts again is silent the second time -- so the
        operator is told once and never again, which is most of the way back to the
        silence #47 exists to remove.
        """
        db = DatabaseManager(str(tmp_path / "second-episode.db"))
        try:
            service, uuid = _rig(db, count=8, config={
                "download_queue_auto_resume_max_attempts": 1})
            _pause(db, service, uuid)
            service._maybe_auto_resume()
            _pause(db, service, uuid)
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            assert any(uuid in r.getMessage() for r in caplog.records
                       if r.levelno >= logging.WARNING), "first episode silent"

            # A real delivery refunds the budget, the batch resumes, then exhausts
            # a SECOND time. That is a new episode and must produce a fresh signal.
            _complete_one(db, service, uuid, method="jdownloader")
            service._maybe_auto_resume()
            _pause(db, service, uuid)
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                service._maybe_auto_resume()
            assert any(uuid in r.getMessage() for r in caplog.records
                       if r.levelno >= logging.WARNING), (
                "the second exhaustion episode produced no warning; keyed on the "
                "batch alone it would be silent forever after the first")
        finally:
            db.close()
