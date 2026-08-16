"""The three design-review findings left unbuilt: F4, F5, F10.

F4  source pacing must be GLOBAL, not per batch
F5  the retry budget must not deadlock
F10 scraper drift must be visible APART from source gating
"""
import uuid

import pytest

from backend.database import DatabaseManager
from backend.download_queue import DownloadQueueService


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


def _attempt(db, *, source="hdencode", reason=None, item=None, started="-1 minute",
             transport=True):
    aid = str(uuid.uuid4())
    db.begin_queue_attempt(aid, item or str(uuid.uuid4()), "b1", source)
    db._mutate("UPDATE download_queue_attempts SET started_at = datetime('now', ?) "
               "WHERE attempt_id = ?", (started, aid), label="test_age")
    db.close_queue_attempt(aid, "FAILED" if reason else "SUCCESS",
                           reason_code=reason, transport_attempted=transport)
    return aid


class TestF4SourcePacingIsGlobal:
    """Batch pacing is DEMAND; the source gate is CAPACITY.

    Without a global gate two concurrent batches each get their own 600s lane
    and hit the source twice as fast as configured -- itself a plausible
    contributor to the gating we then blame on the source.
    """

    def test_the_interval_has_a_floor(self):
        svc = DownloadQueueService.__new__(DownloadQueueService)
        svc.config = {}
        assert svc._source_interval_seconds() >= svc.SOURCE_MIN_INTERVAL_SECONDS

    def test_a_silly_low_config_cannot_disable_the_gate(self):
        """Control: the floor is the protection, so config must not defeat it."""
        svc = DownloadQueueService.__new__(DownloadQueueService)
        svc.config = {"download_source_min_interval_seconds": 0}
        assert svc._source_interval_seconds() == svc.SOURCE_MIN_INTERVAL_SECONDS

    def test_a_higher_config_is_honoured(self):
        svc = DownloadQueueService.__new__(DownloadQueueService)
        svc.config = {"download_source_min_interval_seconds": 900}
        assert svc._source_interval_seconds() == 900

    def test_a_broken_config_falls_back_rather_than_raising(self):
        svc = DownloadQueueService.__new__(DownloadQueueService)
        svc.config = {"download_source_min_interval_seconds": "soon"}
        assert svc._source_interval_seconds() == svc.SOURCE_MIN_INTERVAL_SECONDS


class TestF5BudgetDoesNotDeadlock:
    def test_the_exhausted_window_has_a_floor(self):
        """It restores LIVENESS, not a retry cadence -- so it cannot be tuned
        down into the unbounded slow-motion loop the review warned about."""
        svc = DownloadQueueService.__new__(DownloadQueueService)
        svc.config = {"download_queue_exhausted_retry_window_seconds": 5}
        assert svc._exhausted_retry_window_seconds() >= 3600

    def test_the_default_is_long(self):
        svc = DownloadQueueService.__new__(DownloadQueueService)
        svc.config = {}
        assert svc._exhausted_retry_window_seconds() == 21600


class TestF10ScraperDriftIsSeparate:
    def test_repeated_structural_failures_on_DISTINCT_items_read_as_drift(self, db):
        for _ in range(3):
            _attempt(db, reason="layout_changed")
        r = db.scraper_drift_report()
        assert r["drifting"] is True
        assert r["distinct_items"] == 3

    def test_ONE_bad_release_is_not_a_redesign(self, db):
        """Control: a pulled release must not read as a site-wide change."""
        _attempt(db, reason="layout_changed")
        assert db.scraper_drift_report()["drifting"] is False

    def test_retrying_ONE_item_cannot_manufacture_drift(self, db):
        """DISTINCT is load-bearing here too."""
        same = str(uuid.uuid4())
        for _ in range(6):
            _attempt(db, reason="layout_changed", item=same)
        r = db.scraper_drift_report()
        assert r["distinct_items"] == 1 and r["drifting"] is False

    def test_source_gating_is_NOT_counted_as_drift(self, db):
        """The whole point: a hostile source and a broken selector need
        opposite responses, so they must not share a bucket."""
        for _ in range(5):
            _attempt(db, reason="source_temporarily_blocked")
        assert db.scraper_drift_report()["drifting"] is False

    def test_pages_never_fetched_say_nothing_about_the_template(self, db):
        for _ in range(5):
            _attempt(db, reason="layout_changed", transport=False)
        assert db.scraper_drift_report()["drifting"] is False

    def test_drift_surfaces_in_the_stall_report_as_human_required(self, db):
        for _ in range(3):
            _attempt(db, reason="layout_changed")
        rep = db.queue_stall_report()
        assert rep["scraper_drift"]["drifting"] is True
        assert rep["human_required"] is True


class TestAttemptsRecordTheirRealOutcome:
    """Production had 2 attempt rows and BOTH read FAILED/attempt_not_closed:
    the finally-backstop was the only thing ever closing an attempt. Every
    consumer of these rows -- pacing, stall reporting, drift, source liveness
    -- was therefore reading a queue in which nothing had ever succeeded."""

    def _svc(self, db, result):
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.download_item.return_value = result
        return DownloadQueueService({}, db, fake, broadcast=lambda *a: None)

    def _run_one(self, db, result, **item_kw):
        svc = self._svc(db, result)
        item = {"url": "https://hdencode.org/x", "title": "T", "year": 2020,
                "resolution": "2160p", "size_text": "1 GB", "hdr": "", "dovi": 0,
                "service_type": "Rapidgator", "source": "hdencode"}
        item.update(item_kw)
        svc.schedule_batch([item], interval_minutes=0, mode="immediate")
        claimed = svc._claim_due()
        assert claimed is not None, "nothing claimable -- fixture is vacuous"
        assert claimed["source"] == "hdencode"
        svc._execute(claimed)
        rows = db._query("SELECT * FROM download_queue_attempts", ())
        assert len(rows) == 1
        return rows[0]

    def test_a_real_delivery_is_recorded_as_SUCCESS_not_as_the_backstop(self, db):
        row = self._run_one(db, {"success": True, "method": "jdownloader",
                                 "link_count": 4, "message": "Sent"})
        assert row["terminal_status"] == "SUCCESS"
        assert row["reason_code"] != "attempt_not_closed"
        assert row["transport_attempted"] == 1
        assert row["source_progress"] == 1

    def test_a_cached_duplicate_never_touched_the_source(self, db):
        """It returns before the scraper runs, so it must not spend the lane."""
        row = self._run_one(db, {"success": True, "method": "duplicate",
                                 "link_count": 0, "message": "Already grabbed."})
        assert row["terminal_status"] == "SUCCESS"
        assert row["transport_attempted"] == 0
        assert row["source_progress"] == 0

    def test_a_failure_carries_its_own_reason_not_the_backstops(self, db):
        row = self._run_one(db, {"success": False, "method": "", "link_count": 0,
                                 "message": "no links", "reason_code": "layout_changed",
                                 "stage": "scrape", "transport_attempted": True,
                                 "affected_scope": "item"})
        assert row["terminal_status"] == "FAILED"
        assert row["reason_code"] == "layout_changed"
        assert row["transport_attempted"] == 1
        assert row["source_progress"] == 0

    def test_the_backstop_still_fires_when_nothing_else_can(self, db):
        """Control: removing the real closes must NOT leave attempts open."""
        svc = self._svc(db, {"success": True, "method": "jdownloader",
                             "link_count": 1, "message": "ok"})
        svc._execute_inner = lambda *a, **k: None  # simulate an escaping path
        svc.schedule_batch([{"url": "https://hdencode.org/y", "title": "T2",
                             "year": 2020, "resolution": "2160p", "size_text": "1 GB",
                             "hdr": "", "dovi": 0, "service_type": "Rapidgator",
                             "source": "hdencode"}],
                           interval_minutes=0, mode="immediate")
        svc._execute(svc._claim_due())
        row = db._query("SELECT * FROM download_queue_attempts", ())[0]
        assert row["terminal_status"] == "FAILED"
        assert row["reason_code"] == "attempt_not_closed"


class TestF4TheGateActuallyGates:
    """The floor tests above only prove the NUMBER is right. These prove the
    number is CONNECTED to claiming -- the gap that lets a config test pass
    over a query that ignores it."""

    def _svc(self, db, result):
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.download_item.return_value = result
        return DownloadQueueService({}, db, fake, broadcast=lambda *a: None)

    def _two(self, db, result):
        svc = self._svc(db, result)
        svc.schedule_batch(
            [{"url": "https://hdencode.org/%d" % n, "title": "T%d" % n,
              "year": 2020, "resolution": "2160p", "size_text": "1 GB", "hdr": "",
              "dovi": 0, "service_type": "Rapidgator", "source": "hdencode"}
             for n in (1, 2)],
            interval_minutes=0, mode="immediate")
        first = svc._claim_due()
        assert first is not None, "fixture is vacuous -- nothing was claimable"
        assert first["source"] == "hdencode", (
            "fixture no longer exercises the production source string")
        svc._execute(first)
        return svc

    def test_a_second_source_hit_is_refused_inside_the_window(self, db):
        svc = self._two(db, {"success": True, "method": "jdownloader",
                             "link_count": 3, "message": "Sent"})
        assert svc._claim_due() is None

    def test_the_item_becomes_claimable_once_the_window_passes(self, db):
        """Refused, not dropped: the gate must pace work, not strand it."""
        svc = self._two(db, {"success": True, "method": "jdownloader",
                             "link_count": 3, "message": "Sent"})
        db._mutate("UPDATE download_queue_attempts "
                   "SET started_at = datetime('now', '-2 hours')", (),
                   label="test_age_all")
        assert svc._claim_due() is not None

    def test_work_the_source_never_saw_does_not_close_the_lane(self, db):
        """Same shape, only the outcome differs -- so a pass here cannot come
        from the fixture failing to queue a second item."""
        svc = self._two(db, {"success": True, "method": "duplicate",
                             "link_count": 0, "message": "Already grabbed."})
        assert svc._claim_due() is not None

    def test_an_attempt_still_running_holds_the_lane(self, db):
        """An unfinished attempt has not yet said whether it reached the
        source; the conservative reading is that it did."""
        svc = self._svc(db, {"success": True, "method": "jdownloader",
                             "link_count": 1, "message": "ok"})
        svc.schedule_batch(
            [{"url": "https://hdencode.org/%d" % n, "title": "T%d" % n,
              "year": 2020, "resolution": "2160p", "size_text": "1 GB", "hdr": "",
              "dovi": 0, "service_type": "Rapidgator", "source": "hdencode"}
             for n in (1, 2)],
            interval_minutes=0, mode="immediate")
        first = svc._claim_due()
        db.begin_queue_attempt("open-1", first["item_uuid"], first["batch_uuid"],
                               first["source"])
        assert svc._claim_due() is None


class TestF5TheDeadlockActuallyBreaks:
    """The floor tests prove the window's NUMBER. These drive the REAL sweep:
    an earlier version of this class re-implemented the eligibility SQL inline
    and mutation testing showed it survived deleting the production clause --
    it was testing its own copy."""

    def _svc(self, db):
        from unittest.mock import MagicMock
        svc = DownloadQueueService({}, db, MagicMock(), broadcast=lambda *a: None)
        svc._coordinator_snapshot = lambda: {"blocked": False}
        svc.schedule_batch(
            [{"url": "https://hdencode.org/a", "title": "A", "year": 2020,
              "resolution": "2160p", "size_text": "1 GB", "hdr": "", "dovi": 0,
              "service_type": "Rapidgator", "source": "hdencode"}],
            interval_minutes=0, mode="immediate")
        b = db._query("SELECT batch_uuid FROM download_queue_batches", ())[0]["batch_uuid"]
        db._mutate(
            "UPDATE download_queue_items SET state = 'waiting_source', "
            "queue_reason = 'source_deferred', "
            "cooldown_until = datetime('now', '-1 hour'), "
            "last_reason_code = 'source_temporarily_blocked', "
            "scheduled_for = NULL WHERE batch_uuid = ?", (b,), label="t")
        db._mutate(
            "UPDATE download_queue_batches SET auto_resume_after_cooldown = 1, "
            "auto_resume_used = 99, source_delivery_count = 0, "
            "auto_resume_progress_mark = 0, cooldown_until = NULL, "
            "state = 'paused_source' WHERE batch_uuid = ?", (b,), label="t")
        return svc, b

    def _age(self, db, b, expr):
        db._mutate("UPDATE download_queue_batches SET updated_at = datetime('now', ?) "
                   "WHERE batch_uuid = ?", (expr, b), label="t")

    def _states(self, db, b):
        return {r["state"] for r in db._query_dicts(
            "SELECT state FROM download_queue_items WHERE batch_uuid = ?", (b,),
            default=[])}

    def test_a_spent_budget_with_no_progress_stays_parked_while_fresh(self, db):
        """The bug: a batch needs a DELIVERY to refund its budget and a RETRY
        to get a delivery. Nothing but a human breaks that."""
        svc, b = self._svc(db)
        self._age(db, b, "-1 minute")
        svc._maybe_auto_resume()
        assert self._states(db, b) == {"waiting_source"}

    def test_the_same_batch_revives_once_the_quiet_window_passes(self, db):
        """Identical fixture, one field different -- so a pass here cannot come
        from the sweep being broken outright."""
        svc, b = self._svc(db)
        self._age(db, b, "-7 hours")
        svc._maybe_auto_resume()
        assert self._states(db, b) != {"waiting_source"}

    def test_the_window_is_not_a_counter_reset(self, db):
        """The review's warning: refunding the budget on a timer turns a finite
        retry budget into an unbounded loop in slow motion. Revival must not
        hand the batch its allowance back."""
        svc, b = self._svc(db)
        self._age(db, b, "-7 hours")
        svc._maybe_auto_resume()
        used = db._query_dicts("SELECT auto_resume_used AS u FROM "
                               "download_queue_batches WHERE batch_uuid = ?", (b,),
                               default=[])[0]["u"]
        assert used >= 99, "the timer refunded the budget"

    def test_a_held_source_is_still_refused(self, db):
        """The window widens DISCOVERY only -- _resume_batch re-reads the hold
        and is the single authority, so no timer may release one."""
        svc, b = self._svc(db)
        self._age(db, b, "-7 hours")
        db._mutate("UPDATE download_queue_batches SET verification_hold_source = "
                   "'hdencode' WHERE batch_uuid = ?", (b,), label="t")
        svc._maybe_auto_resume()
        assert self._states(db, b) == {"waiting_source"},             "a timer released a verification hold"

    def test_an_unknown_outcome_is_never_revived_by_the_timer(self, db):
        """Retrying something that may already have happened is worse than
        leaving it parked; the window must not weaken that."""
        svc, b = self._svc(db)
        self._age(db, b, "-7 hours")
        db._mutate("UPDATE download_queue_items SET last_reason_code = "
                   "'interrupted_unknown_outcome' WHERE batch_uuid = ?", (b,),
                   label="t")
        svc._maybe_auto_resume()
        assert self._states(db, b) == {"waiting_source"}


class TestF5AuthorityFailsClosed:
    """The sweep tests above cannot prove the NEGATIVE case: discovery already
    refuses a fresh batch, so the authority is never consulted and mutating it
    changes nothing. Mutation testing showed exactly that -- two mutants of
    _quiet_long_enough survived the sweep tests. The policy is pure, so ask it
    directly."""

    def _facts(self, **kw):
        import datetime as dt
        from backend.queue_recovery_policy import ItemFacts, SharedFacts
        now = dt.datetime.now(dt.timezone.utc)
        item = ItemFacts(state="waiting_source",
                         cooldown_until=now - dt.timedelta(hours=1),
                         queue_reason="source_deferred", last_reason_code="")
        shared = dict(cooldown_until=None, auto_resume_enabled=True,
                      attempts_used=99, source_delivery_count=0, progress_mark=0,
                      max_attempts=3, verification_hold=False,
                      quiet_since=now - dt.timedelta(hours=7),
                      exhausted_retry_window_seconds=21600)
        shared.update(kw)
        return item, SharedFacts(**shared), now

    def _decide(self, **kw):
        from backend.queue_recovery_policy import decide
        item, shared, now = self._facts(**kw)
        return decide(item, shared, now=now)

    def test_a_long_quiet_batch_is_authorised(self):
        """Positive control: without this the negatives below are vacuous."""
        from backend.queue_recovery_policy import AUTHORISED
        assert self._decide() == AUTHORISED

    def test_a_recently_touched_batch_is_still_budget_spent(self):
        import datetime as dt
        from backend.queue_recovery_policy import BUDGET_SPENT
        assert self._decide(
            quiet_since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        ) == BUDGET_SPENT

    def test_no_timestamp_fails_CLOSED(self):
        from backend.queue_recovery_policy import BUDGET_SPENT
        assert self._decide(quiet_since=None) == BUDGET_SPENT

    def test_no_window_fails_CLOSED(self):
        from backend.queue_recovery_policy import BUDGET_SPENT
        assert self._decide(exhausted_retry_window_seconds=None) == BUDGET_SPENT

    def test_a_zero_window_is_not_an_open_door(self):
        """A stall someone can see beats a silent forever-retry."""
        from backend.queue_recovery_policy import BUDGET_SPENT
        assert self._decide(exhausted_retry_window_seconds=0) == BUDGET_SPENT

    def test_an_unparsable_window_fails_CLOSED(self):
        from backend.queue_recovery_policy import BUDGET_SPENT
        assert self._decide(exhausted_retry_window_seconds="six hours") == BUDGET_SPENT

    def test_a_naive_timestamp_is_read_as_UTC_not_crashed_on(self):
        """sqlite hands back naive strings; _parse may too. Comparing those to
        an aware `now` raises, and a raising policy is an outage."""
        import datetime as dt
        from backend.queue_recovery_policy import AUTHORISED
        assert self._decide(
            quiet_since=dt.datetime.utcnow() - dt.timedelta(hours=7)) == AUTHORISED

    def test_the_hold_still_outranks_a_quiet_batch(self):
        from backend.queue_recovery_policy import VERIFICATION_HOLD
        assert self._decide(verification_hold=True) == VERIFICATION_HOLD

    def test_the_window_does_not_override_a_disabled_batch(self):
        from backend.queue_recovery_policy import DISABLED
        assert self._decide(auto_resume_enabled=False) == DISABLED

    def test_the_window_does_not_override_the_shared_brake(self):
        import datetime as dt
        from backend.queue_recovery_policy import WAITING_BRAKE
        assert self._decide(
            cooldown_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        ) == WAITING_BRAKE


class TestAmbiguousDeferralActuallyWrites:
    """_defer_item_only wrote queue_reason='item_retry', which the column's
    CHECK rejects -- so the method that exists to stop an ambiguous denial
    turning 78 items into permanent failures raised on EVERY call. Present on
    main and deployed. The existing tests asserted on the resulting row, and a
    write that raises leaves no row to assert on."""

    def _svc(self, db):
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.download_item.return_value = {
            "success": False, "method": "", "link_count": 0,
            "message": "The reveal did not complete.",
            "reason_code": "reveal_verification_stalled", "stage": "reveal",
            "retryable": True, "retry_mode": "auto", "transport_attempted": True,
            "affected_scope": "source", "action_code": "retry", "signals": [],
        }
        svc = DownloadQueueService({}, db, fake, broadcast=lambda *a: None)
        svc.schedule_batch(
            [{"url": "https://hdencode.org/one", "title": "One", "year": 2020,
              "resolution": "2160p", "size_text": "1 GB", "hdr": "", "dovi": 0,
              "service_type": "Rapidgator", "source": "hdencode"}],
            interval_minutes=0, mode="immediate")
        return svc

    def test_deferring_one_ambiguous_item_does_not_raise(self, db):
        svc = self._svc(db)
        item = svc._claim_due()
        assert item is not None
        svc._execute(item)          # raised sqlite3.IntegrityError before the fix
        row = db._query_dicts("SELECT state, queue_reason FROM "
                              "download_queue_items WHERE item_uuid = ?",
                              (item["item_uuid"],), default=[])[0]
        assert row["state"] == "ready", "the item must stay runnable, not fail"
        assert row["queue_reason"] == "source_deferred"

    def test_the_deferral_is_recorded_as_a_deliberate_skip_not_a_failure(self, db):
        """Counting a policy deferral as an observed source failure is how one
        ambiguous item talks the system into believing the source is down."""
        svc = self._svc(db)
        svc._execute(svc._claim_due())
        row = db._query("SELECT * FROM download_queue_attempts", ())[0]
        assert row["terminal_status"] == "INTENTIONALLY_SKIPPED"
        assert row["reason_code"] == "reveal_verification_stalled"

    def test_the_batch_is_not_paused_by_one_ambiguous_item(self, db):
        svc = self._svc(db)
        svc._execute(svc._claim_due())
        state = db._query_dicts("SELECT state FROM download_queue_batches", (),
                                default=[])[0]["state"]
        assert state != "paused_source"


class TestPacingThrottlesTheMachineNotTheOperator:
    """The gate refused a human's 'Retry now' for up to 60s. That is worse than
    slow, it is INVISIBLE: the API accepts the retry, the worker then declines
    to claim it, and the UI has already reported success. Five verification-hold
    tests caught this -- tests about the hold, not about pacing."""

    def _svc(self, db):
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.download_item.return_value = {"success": True, "method": "jdownloader",
                                           "link_count": 2, "message": "Sent"}
        svc = DownloadQueueService({}, db, fake, broadcast=lambda *a: None)
        svc.schedule_batch(
            [{"url": "https://hdencode.org/%d" % n, "title": "T%d" % n, "year": 2020,
              "resolution": "2160p", "size_text": "1 GB", "hdr": "", "dovi": 0,
              "service_type": "Rapidgator", "source": "hdencode"} for n in (1, 2)],
            interval_minutes=0, mode="immediate")
        first = svc._claim_due()
        assert first is not None
        svc._execute(first)              # spends the source lane
        return svc

    def _remaining(self, db):
        return db._query_dicts(
            "SELECT item_uuid FROM download_queue_items WHERE state IN "
            "('scheduled','ready') ORDER BY sequence_number", (), default=[])[0]

    def test_an_automatic_row_is_still_paced(self, db):
        """Positive control: without this the exemption test proves nothing."""
        svc = self._svc(db)
        assert svc._claim_due() is None

    def test_a_human_promoted_row_is_claimed_immediately(self, db):
        svc = self._svc(db)
        row = self._remaining(db)
        db._mutate("UPDATE download_queue_items SET queue_reason = 'manual_retry' "
                   "WHERE item_uuid = ?", (row["item_uuid"],), label="t")
        claimed = svc._claim_due()
        assert claimed is not None, "a human pressed Retry and the worker refused"
        assert claimed["item_uuid"] == row["item_uuid"]

    def test_the_exemption_is_spelled_the_same_way_retry_item_writes_it(self, db):
        """The gate keys on a literal string. If retry_item ever writes a
        different one the exemption silently stops applying, and the symptom is
        a button that does nothing for a minute -- nobody would connect that to
        this line."""
        svc = self._svc(db)
        row = self._remaining(db)
        svc.retry_item(row["item_uuid"])
        after = db._query_dicts("SELECT queue_reason FROM download_queue_items "
                                "WHERE item_uuid = ?", (row["item_uuid"],),
                                default=[])[0]["queue_reason"]
        assert after == "manual_retry", (
            f"retry_item now writes {after!r}; the pacing exemption keys on "
            "'manual_retry' and would no longer apply")
        assert svc._claim_due() is not None

    def test_only_human_paths_can_reach_the_exemption(self, db):
        """The gate keys on queue_reason='manual_retry', and TWO AUTOMATIC paths
        also write that value -- recover_interrupted() and
        _recover_expired_claim(). They are harmless only because both also write
        state='failed', which _claim_due never selects. That is load-bearing and
        entirely implicit, so it is pinned here: if either is ever changed to
        leave a row runnable, it silently gains a pacing bypass, and the symptom
        (a restart briefly ignoring source pacing) would be near-impossible to
        trace back to this line."""
        import re
        src = open("backend/download_queue.py", encoding="utf-8").read()
        for fn in ("recover_interrupted", "_recover_expired_claim"):
            start = src.index("def %s(" % fn)
            nxt = re.search(r"\n    (?:async )?def ", src[start:])
            body = src[start:start + (nxt.start() if nxt else len(src))]
            assert "queue_reason = 'manual_retry'" in body, (
                f"{fn} no longer writes manual_retry; re-check this pin")
            assert "state = 'failed'" in body, (
                f"{fn} writes queue_reason='manual_retry' but no longer parks the "
                "row as 'failed'. If it is now claimable it BYPASSES source "
                "pacing, because the gate treats manual_retry as 'a human asked'.")
