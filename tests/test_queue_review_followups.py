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
             transport=True, progress=None):
    """One closed attempt row, aged by `started`.

    `progress` defaults to "a success delivered": source_progress is the
    SOURCE-LIVENESS signal, and an attempt that succeeded without setting it is
    not a thing production produces. Leaving it 0 by default made a fixture
    "delivery" invisible to the episode logic that reads it, so a test asserting
    that a delivery closes a no-progress episode failed against correct code.
    Pass progress=False for a success that never reached the source, e.g. a
    cache-resolved duplicate.
    """
    aid = str(uuid.uuid4())
    db.begin_queue_attempt(aid, item or str(uuid.uuid4()), "b1", source)
    db._mutate("UPDATE download_queue_attempts SET started_at = datetime('now', ?) "
               "WHERE attempt_id = ?", (started, aid), label="test_age")
    db.close_queue_attempt(aid, "FAILED" if reason else "SUCCESS",
                           reason_code=reason, transport_attempted=transport,
                           source_progress=(not reason) if progress is None
                           else progress)
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


class TestTransportIsDeclaredPerCode:
    """The 2026-08-16 review blocker: LAYOUT_CHANGED and REVEAL_CONTROL_ABSENT
    are decided only AFTER the page is fetched, but neither construction site
    passed transport_attempted, so it defaulted to None -> bool(None) is False
    -> persisted as 0 -> scraper_drift_report (which counts only 1) could never
    see a real structural failure. F10 shipped inert, and its own tests passed
    because they insert attempt rows directly instead of using the producer."""

    def test_every_scrape_code_declares_transport(self):
        """An omission must be a BUILD ERROR, not a silent False. This is the
        whole point of moving the answer off the call sites: nine of fourteen
        sites had quietly omitted it."""
        from backend.scrape_outcome import ScrapeCode, _TRANSPORT_BY_CODE
        missing = [c.name for c in ScrapeCode if c not in _TRANSPORT_BY_CODE]
        assert not missing, (
            "ScrapeCode(s) with no declared transport semantics: %s. Add an entry "
            "to _TRANSPORT_BY_CODE -- defaulting silently is how F10 shipped "
            "unable to see its own evidence." % missing)

    def test_the_structural_codes_report_contact(self):
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        for code in (ScrapeCode.LAYOUT_CHANGED, ScrapeCode.REVEAL_CONTROL_ABSENT):
            d = ScrapeDiagnostic(code, retryable=False, affects_source_health=True)
            assert d.to_dict()["transport_attempted"] is True, code.name

    def test_the_pre_request_refusals_report_no_contact(self):
        """Control: if everything said True the flag would carry no information."""
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        for code in (ScrapeCode.SOURCE_DISABLED,
                     ScrapeCode.SOURCE_TEMPORARILY_BLOCKED,
                     ScrapeCode.BROWSER_LAUNCH_FAILED,
                     ScrapeCode.UNSUPPORTED_SOURCE,
                     ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE):
            d = ScrapeDiagnostic(code)
            assert d.to_dict()["transport_attempted"] is False, code.name

    def test_an_explicit_value_still_wins(self):
        """A site that KNOWS nothing was sent must be able to say so."""
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        d = ScrapeDiagnostic(ScrapeCode.SCRAPE_EXCEPTION, transport_attempted=False)
        assert d.to_dict()["transport_attempted"] is False

    def test_the_flag_is_never_None_on_the_wire(self):
        """Every consumer coerces with bool(), so None silently becomes False --
        an assertion of 'no request was made' that nobody wrote."""
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        for code in ScrapeCode:
            assert ScrapeDiagnostic(code).to_dict()["transport_attempted"] in (True, False), code.name

    def test_a_real_structural_failure_reaches_the_drift_report(self, db):
        """The seam the blocker lived in: producer -> queue -> attempt row ->
        scraper_drift_report. Every earlier F10 test started halfway along it."""
        from unittest.mock import MagicMock
        from backend.download_outcome import public_download_result
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic

        for n in range(3):
            fake = MagicMock()
            fake.download_item.return_value = public_download_result(
                {"success": False, "method": "", "link_count": 0,
                 **ScrapeDiagnostic(ScrapeCode.LAYOUT_CHANGED, retryable=False,
                                    affects_source_health=True).to_dict()},
                title="T%d" % n, url="https://hdencode.org/%d" % n)
            svc = DownloadQueueService({}, db, fake, broadcast=lambda *a: None)
            svc.schedule_batch(
                [{"url": "https://hdencode.org/%d" % n, "title": "T%d" % n,
                  "year": 2020, "resolution": "2160p", "size_text": "1 GB",
                  "hdr": "", "dovi": 0, "service_type": "Rapidgator",
                  "source": "hdencode"}], interval_minutes=0, mode="immediate")
            item = svc._claim_due()
            assert item is not None, "claim %d refused; fixture is vacuous" % n
            svc._execute(item)
            db._mutate("UPDATE download_queue_attempts SET started_at = "
                       "datetime('now', '-1 minute')", (), label="t")

        rows = db._query_dicts(
            "SELECT reason_code, transport_attempted FROM download_queue_attempts",
            (), default=[])
        assert all(r["transport_attempted"] == 1 for r in rows), (
            "the producer still records structural failures as 'never contacted "
            "the source': %s" % rows)
        report = db.scraper_drift_report()
        assert report["drifting"] is True, report
        assert report["distinct_items"] == 3, report


class TestPeerReviewFixes:
    """The 2026-08-16 ChatGPT round, beyond the F10 blocker."""

    def _svc(self, db, n=3, result=None):
        from unittest.mock import MagicMock
        fake = MagicMock()
        fake.download_item.return_value = result or {
            "success": True, "method": "jdownloader", "link_count": 2,
            "message": "Sent"}
        svc = DownloadQueueService({}, db, fake, broadcast=lambda *a: None)
        svc.schedule_batch(
            [{"url": "https://hdencode.org/%d" % i, "title": "T%d" % i,
              "year": 2020, "resolution": "2160p", "size_text": "1 GB",
              "hdr": "", "dovi": 0, "service_type": "Rapidgator",
              "source": "hdencode"} for i in range(n)],
            interval_minutes=0, mode="immediate")
        return svc

    def _spend_the_lane(self, svc):
        first = svc._claim_due()
        assert first is not None
        svc._execute(first)
        assert svc._claim_due() is None, "the lane was not spent; test is vacuous"

    def _stall(self):
        return {"success": False, "method": "", "link_count": 0,
                "message": "stalled", "reason_code": "reveal_verification_stalled",
                "stage": "reveal", "retryable": True, "retry_mode": "auto",
                "transport_attempted": True, "affected_scope": "source",
                "action_code": "retry", "signals": []}

    # --- the exemption must not open the gate for a BULK retry ---------------

    def test_one_manual_row_is_exempt(self, db):
        """Positive control for the two negatives below."""
        svc = self._svc(db)
        self._spend_the_lane(svc)
        row = db._query_dicts("SELECT item_uuid FROM download_queue_items "
                              "WHERE state IN ('scheduled','ready') "
                              "ORDER BY sequence_number LIMIT 1", (), default=[])[0]
        db._mutate("UPDATE download_queue_items SET queue_reason = 'manual_retry' "
                   "WHERE item_uuid = ?", (row["item_uuid"],), label="t")
        assert svc._claim_due() is not None

    def test_a_BULK_manual_retry_is_still_paced(self, db):
        """retry_ready() and a manual resume stamp the SAME marker on every row.
        A blanket exemption let one tap send N items at the source that was
        already refusing -- the stampede the gate exists to prevent."""
        svc = self._svc(db)
        self._spend_the_lane(svc)
        db._mutate("UPDATE download_queue_items SET queue_reason = 'manual_retry' "
                   "WHERE state IN ('scheduled','ready')", (), label="t")
        due = db._query_dicts("SELECT COUNT(*) AS n FROM download_queue_items "
                              "WHERE queue_reason='manual_retry' AND state IN "
                              "('scheduled','ready')", (), default=[])[0]["n"]
        assert due >= 2, "fixture must promote MORE than one row"
        assert svc._claim_due() is None, (
            "a bulk manual promotion bypassed source pacing")

    def test_retry_ready_does_not_bypass_pacing(self, db):
        """The real bulk path, not a hand-written UPDATE."""
        svc = self._svc(db, n=3)
        self._spend_the_lane(svc)
        db._mutate("UPDATE download_queue_items SET state='waiting_source', "
                   "queue_reason='source_deferred', "
                   "cooldown_until=datetime('now','-1 hour') "
                   "WHERE state IN ('scheduled','ready')", (), label="t")
        svc.retry_ready(interval_minutes=0)
        assert svc._claim_due() is None, (
            "Retry all ready opened the source lane for the whole batch")

    # --- IN_PROGRESS holds by the CLAIM LEASE, not the pacing interval -------

    def test_a_live_claim_holds_the_lane_past_the_pacing_interval(self, db):
        svc = self._svc(db)
        first = svc._claim_due()
        db.begin_queue_attempt("open-1", first["item_uuid"], first["batch_uuid"],
                               first["source"])
        db._mutate("UPDATE download_queue_attempts SET started_at = "
                   "datetime('now', '-2 hours') WHERE attempt_id = 'open-1'",
                   (), label="t")
        assert svc._claim_due() is None, (
            "an unfinished attempt stopped holding the lane after the pacing "
            "interval instead of while its claim was live")

    def test_an_EXPIRED_claim_stops_holding_the_lane(self, db):
        """Control: otherwise a dead worker would block the source forever."""
        svc = self._svc(db)
        first = svc._claim_due()
        db.begin_queue_attempt("open-2", first["item_uuid"], first["batch_uuid"],
                               first["source"])
        db._mutate("UPDATE download_queue_attempts SET started_at = "
                   "datetime('now', '-2 hours') WHERE attempt_id = 'open-2'",
                   (), label="t")
        db._mutate("UPDATE download_queue_items SET claim_expires_at = ? "
                   "WHERE item_uuid = ?",
                   ("1999-01-01T00:00:00+00:00", first["item_uuid"]), label="t")
        assert svc._claim_due() is not None

    # --- _defer_item_only ownership discipline ------------------------------

    def test_a_deferral_does_not_double_count_the_attempt(self, db):
        """_claim_due already incremented attempt_count when it claimed."""
        svc = self._svc(db, n=1, result=self._stall())
        item = svc._claim_due()
        svc._execute(item)
        row = db._query_dicts("SELECT attempt_count FROM download_queue_items "
                              "WHERE item_uuid = ?", (item["item_uuid"],),
                              default=[])[0]
        assert row["attempt_count"] == 1, (
            "one attempt was counted %d times" % row["attempt_count"])

    def test_a_deferral_releases_the_claim(self, db):
        svc = self._svc(db, n=1, result=self._stall())
        item = svc._claim_due()
        svc._execute(item)
        row = db._query_dicts("SELECT state, claimed_by, claim_expires_at FROM "
                              "download_queue_items WHERE item_uuid = ?",
                              (item["item_uuid"],), default=[])[0]
        assert row["state"] == "ready"
        assert row["claimed_by"] is None and row["claim_expires_at"] is None, (
            "a row put back to ready is still owned by a finished worker")

    def test_a_deferral_cannot_overwrite_the_watchdogs_safety_state(self, db):
        """THE RACE: once the lease expires the watchdog writes
        operation_timeout_unknown, whose whole meaning is 'we do not know
        whether the delivery happened'. A late worker must not resurrect it to
        ready and risk duplicating a delivery that already succeeded."""
        svc = self._svc(db, n=1, result=self._stall())
        item = svc._claim_due()
        db._mutate("UPDATE download_queue_items SET state='failed', "
                   "claimed_by=NULL, last_reason_code='operation_timeout_unknown' "
                   "WHERE item_uuid = ?", (item["item_uuid"],), label="t")
        assert svc._defer_item_only(item, self._stall()) is False
        row = db._query_dicts("SELECT state, last_reason_code FROM "
                              "download_queue_items WHERE item_uuid = ?",
                              (item["item_uuid"],), default=[])[0]
        assert row["state"] == "failed"
        assert row["last_reason_code"] == "operation_timeout_unknown"

    # --- F10 distinct is GLOBAL ---------------------------------------------

    def test_one_item_failing_two_structural_ways_counts_once(self, db):
        """Summing per-reason DISTINCT counts let ONE release contribute 2
        toward a threshold documented as three distinct items."""
        same = str(uuid.uuid4())
        _attempt(db, reason="layout_changed", item=same)
        _attempt(db, reason="reveal_control_absent", item=same)
        r = db.scraper_drift_report()
        assert r["distinct_items"] == 1, r
        assert r["drifting"] is False
        assert r["by_reason"] == {"layout_changed": 1, "reveal_control_absent": 1}

    # --- the starvation alert can actually fire -----------------------------

    def test_the_starvation_alert_can_fire_on_the_same_day(self, db):
        """It compared scheduled_for (ISO T) against sqlite's space format, so
        on any given day it was false all day. Dead since it was written."""
        self._svc(db, n=1)
        db._mutate("UPDATE download_queue_items SET state='ready', "
                   "scheduled_for = REPLACE(datetime('now','-3 hours'),' ','T') "
                   "|| '+00:00'", (), label="t")
        rep = db.queue_stall_report()
        assert rep["executor_starved"] is True, (
            "work due 3 hours ago with no attempt is starvation: %s" % rep)

    def test_an_attempt_that_started_after_the_due_time_is_not_starvation(self, db):
        """Control: with the fix, a real attempt must clear the alarm."""
        self._svc(db, n=1)
        db._mutate("UPDATE download_queue_items SET state='ready', "
                   "scheduled_for = REPLACE(datetime('now','-3 hours'),' ','T') "
                   "|| '+00:00'", (), label="t")
        _attempt(db, started="-1 hour")
        rep = db.queue_stall_report()
        assert rep["executor_starved"] is False, rep


class TestSourceNoProgressNeedsAnEpisode:
    """Round-2 peer review, the one finding left open.

    `source_no_progress` used to be: does ANY attempt row exist, and is
    COALESCE(last_progress, '1970-01-01') older than the deadline? With no
    delivery ever recorded the fallback is the epoch, so the FIRST failed
    attempt in a fresh history set it immediately -- contradicting the key's own
    stated contract. And `last_attempt_at` was tested only for EXISTENCE, so a
    months-old attempt satisfied it during a current starvation, letting
    executor_starved and source_no_progress both be true at once. Those two are
    the whole reason this report exists.
    """

    def _due_item(self, db):
        """One item due 3 hours ago, in the shape download_queue._iso() writes."""
        from unittest.mock import MagicMock
        svc = DownloadQueueService({}, db, MagicMock(), broadcast=lambda *a: None)
        svc.schedule_batch(
            [{"url": "https://hdencode.org/x", "title": "T", "year": 2020,
              "resolution": "2160p", "size_text": "1 GB", "hdr": "", "dovi": 0,
              "service_type": "Rapidgator", "source": "hdencode"}],
            interval_minutes=0, mode="immediate")
        db._mutate("UPDATE download_queue_items SET state='ready', "
                   "scheduled_for = REPLACE(datetime('now','-3 hours'),' ','T') "
                   "|| '+00:00'", (), label="t")
        return svc

    def test_ONE_fresh_failure_with_no_history_is_not_a_dead_source(self, db):
        """The reported defect. One attempt, seconds old, nothing delivered yet
        -- that is a normal first try, not evidence the source is gone."""
        self._due_item(db)
        _attempt(db, reason="layout_changed", started="-1 minute")
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is False, rep

    def test_failing_for_longer_than_the_deadline_IS_a_dead_source(self, db):
        """Positive control: without this the test above proves only that the
        key never fires."""
        self._due_item(db)
        for age in ("-5 hours", "-3 hours", "-10 minutes"):
            _attempt(db, reason="layout_changed", started=age)
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is True, rep

    def test_starvation_with_only_STALE_history_is_not_a_source_fault(self, db):
        """The two diagnoses must not coexist because of old history: work is
        due and NOTHING is being attempted, which is a scheduler fault."""
        self._due_item(db)
        _attempt(db, reason="layout_changed", started="-30 days")
        rep = db.queue_stall_report()
        assert rep["executor_starved"] is True, rep
        assert rep["source_no_progress"] is False, (
            "a 30-day-old attempt made a current starvation look like the "
            "source's fault: %s" % rep)

    def test_a_delivery_ends_the_episode(self, db):
        """Failures BEFORE the last delivery belong to a closed episode."""
        self._due_item(db)
        _attempt(db, reason="layout_changed", started="-8 hours")
        _attempt(db, started="-4 hours")          # SUCCESS, source_progress=1
        _attempt(db, reason="layout_changed", started="-5 minutes")
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is False, (
            "an episode that a real delivery already closed still counts: %s" % rep)

    def test_a_delivery_that_is_itself_old_reopens_nothing_on_its_own(self, db):
        """Control for the above: same shape, but the failures AFTER the
        delivery now span the deadline, so the episode is genuinely open."""
        self._due_item(db)
        _attempt(db, started="-9 hours")          # SUCCESS
        _attempt(db, reason="layout_changed", started="-6 hours")
        _attempt(db, reason="layout_changed", started="-5 minutes")
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is True, rep

    def test_policy_deferrals_are_not_evidence_about_the_source(self, db):
        """Rows that never opened a page cannot make the source look dead."""
        self._due_item(db)
        for age in ("-5 hours", "-10 minutes"):
            _attempt(db, reason="source_temporarily_blocked", started=age,
                     transport=False)
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is False, rep

    def test_the_episode_start_is_reported_as_evidence(self, db):
        """A diagnosis nobody can check is worth little."""
        self._due_item(db)
        _attempt(db, reason="layout_changed", started="-5 hours")
        _attempt(db, reason="layout_changed", started="-5 minutes")
        rep = db.queue_stall_report()
        assert rep["evidence"]["no_progress_episode_since"] is not None

    def test_policy_skips_do_not_count_as_STILL_ASKING(self, db):
        """We stopped asking 5 hours ago; everything since is the queue
        declining to send. That is not 'attempts are happening'.

        Distinguishes the transport filter on the RECENT query specifically:
        the episode query alone cannot keep this False, because a real request
        DID open the episode.
        """
        self._due_item(db)
        _attempt(db, reason="layout_changed", started="-5 hours")
        _attempt(db, reason="source_temporarily_blocked", started="-10 minutes",
                 transport=False)
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is False, (
            "policy deferrals were counted as us still asking: %s" % rep)

    def test_the_episode_begins_when_we_actually_ASKED(self, db):
        """A policy deferral 5 hours ago did not open a no-progress episode --
        nothing was sent. The episode begins at the first REAL request, 10
        minutes ago, which is well inside the deadline.

        Distinguishes the transport filter on the EPISODE query specifically:
        the recent query alone cannot keep this False, because a real request
        did happen recently.
        """
        self._due_item(db)
        _attempt(db, reason="source_temporarily_blocked", started="-5 hours",
                 transport=False)
        _attempt(db, reason="layout_changed", started="-10 minutes")
        rep = db.queue_stall_report()
        assert rep["source_no_progress"] is False, (
            "a policy deferral backdated the start of the episode: %s" % rep)


class TestScopeIsDeclaredPerCode:
    """Second pass of the structural programme, on the same principle as
    transport: `is_source_wide_denial()` is an AND of TWO registries --
    `affected_scope == 'source'` AND `reason_code in _SOURCE_WIDE_REASONS`.

    Not a live bug today (all four source-wide codes do pass the scope), but a
    live TRAP: a code added to the set and constructed without the scope routes
    to _fail instead of _pause_for_source, which is how 78 items became
    permanent failures. Two registries answering one question is also how
    `_source()` drifted on 2026-08-07.
    """

    def test_every_scrape_code_declares_a_scope(self):
        from backend.scrape_outcome import ScrapeCode, _SCOPE_BY_CODE
        missing = [c.name for c in ScrapeCode if c not in _SCOPE_BY_CODE]
        assert not missing, (
            "ScrapeCode(s) with no declared scope: %s. Add an entry to "
            "_SCOPE_BY_CODE; defaulting to 'item' silently is what made routing "
            "depend on every author remembering." % missing)

    def test_only_source_or_item(self):
        from backend.scrape_outcome import _SCOPE_BY_CODE
        assert set(_SCOPE_BY_CODE.values()) <= {"source", "item"}

    def test_the_routing_set_is_DERIVED_not_a_second_copy(self):
        """The whole point: the set and the per-diagnostic value cannot disagree
        because there is only one of them."""
        from backend.scrape_outcome import SOURCE_WIDE_CODES, _SCOPE_BY_CODE
        from backend.download_outcome import _SOURCE_WIDE_REASONS
        assert set(_SOURCE_WIDE_REASONS) == set(SOURCE_WIDE_CODES)
        assert set(SOURCE_WIDE_CODES) == {
            c.value for c, s in _SCOPE_BY_CODE.items() if s == "source"}

    def test_a_bare_source_wide_diagnostic_routes_as_source_wide(self):
        """THE TRAP, closed. Before this, ScrapeDiagnostic(INTERACTIVE_CHALLENGE)
        with no explicit scope yielded affected_scope='item', so
        is_source_wide_denial returned False for a code that IS source-wide --
        it only worked because every call site happened to remember."""
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        from backend.download_outcome import (is_source_wide_denial,
                                              public_download_result)
        for code in (ScrapeCode.SOURCE_DISABLED,
                     ScrapeCode.SOURCE_TEMPORARILY_BLOCKED,
                     ScrapeCode.INTERACTIVE_CHALLENGE,
                     ScrapeCode.REVEAL_VERIFICATION_STALLED):
            result = public_download_result(
                {"success": False, "method": "", "link_count": 0,
                 **ScrapeDiagnostic(code).to_dict()},
                title="T", url="https://hdencode.org/x")
            assert result["affected_scope"] == "source", code.name
            assert is_source_wide_denial(result) is True, code.name

    def test_an_item_scoped_code_does_NOT_route_as_source_wide(self):
        """Control: if everything read as source-wide, one bad page would park
        the whole queue -- the failure the scope exists to prevent."""
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        from backend.download_outcome import (is_source_wide_denial,
                                              public_download_result)
        for code in (ScrapeCode.LAYOUT_CHANGED, ScrapeCode.REVEAL_CONTROL_ABSENT,
                     ScrapeCode.NO_FILE_HOST_LINKS, ScrapeCode.SCRAPE_EXCEPTION,
                     ScrapeCode.BROWSER_LAUNCH_FAILED):
            result = public_download_result(
                {"success": False, "method": "", "link_count": 0,
                 **ScrapeDiagnostic(code).to_dict()},
                title="T", url="https://hdencode.org/x")
            assert result["affected_scope"] == "item", code.name
            assert is_source_wide_denial(result) is False, code.name

    def test_a_local_browser_fault_is_not_blamed_on_the_source(self):
        """A broken browser affects every item, but the fault is OURS. Calling
        it source-wide would pause HDEncode and hide a local outage behind a
        message about the source."""
        from backend.scrape_outcome import ScrapeCode, _SCOPE_BY_CODE
        for code in (ScrapeCode.BROWSER_LAUNCH_FAILED,
                     ScrapeCode.BROWSER_NETWORK_ERROR,
                     ScrapeCode.BROWSER_NAVIGATION_FAILED):
            assert _SCOPE_BY_CODE[code] == "item", code.name

    def test_an_explicit_scope_still_wins(self):
        from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic
        d = ScrapeDiagnostic(ScrapeCode.LAYOUT_CHANGED, affected_scope="source")
        assert d.to_dict()["affected_scope"] == "source"

    def test_the_declared_scope_matches_what_production_actually_passes(self):
        """The table must describe the code as it is, not as I would like it.
        Every construction site that DOES pass affected_scope must agree with
        the declaration -- otherwise the default is a lie for that code."""
        import ast, glob
        from backend.scrape_outcome import ScrapeCode, _SCOPE_BY_CODE
        by_value = {c.value: c for c in ScrapeCode}
        disagreements = []
        for path in glob.glob("backend/*.py"):
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "id", None) == "ScrapeDiagnostic"):
                    continue
                if not node.args or not isinstance(node.args[0], ast.Attribute):
                    continue
                name = node.args[0].attr
                code = getattr(ScrapeCode, name, None)
                if code is None:
                    continue
                for kw in node.keywords:
                    if kw.arg == "affected_scope" and isinstance(kw.value, ast.Constant):
                        if kw.value.value != _SCOPE_BY_CODE[code]:
                            disagreements.append(
                                "%s:%d %s passes %r, table says %r"
                                % (path, node.lineno, name, kw.value.value,
                                   _SCOPE_BY_CODE[code]))
        assert not disagreements, "\n".join(disagreements)
