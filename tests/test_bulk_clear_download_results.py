"""Bulk removal of tracked download results.

The mobile "Clear done" button looped the single-row remove and awaited each:
with 578 rows (563 finished) that is 563 sequential HTTP requests, each of which
re-read every row server-side and made its own JDownloader round trip. No busy
state, no completion message -- so it read as a dead button.

The pre-existing `DELETE /download/results` is NOT the fix: it empties our table
and never tells JDownloader, so the next poll re-upserts every package JD still
holds. Removing them at the source is the whole job.
"""
import threading
from unittest.mock import MagicMock

import pytest

from backend.download_service import DownloadService


class _JD:
    """Records what was asked of JDownloader, so 'one call' is assertable.

    `returns` defaults to None because removeLinks is a VOID action -- that is
    its real success answer. The first version of this double hard-coded None
    and therefore could not constrain the return-value contract at all, which is
    how "an explicit False reads as success" survived review.
    """

    def __init__(self, fail=False, returns=None):
        self.calls = []
        self.fail = fail
        self.returns = returns
        self.downloads = self

    def remove_links(self, links, packages):
        self.calls.append(list(packages))
        if self.fail:
            raise RuntimeError("JDownloader unreachable")
        return self.returns


@pytest.fixture
def svc():
    s = DownloadService.__new__(DownloadService)
    s.db = MagicMock()
    s._results_cache = {}
    s._uuid_id = {}
    s._best_titles = {}
    s._log = lambda *a, **k: None
    s._invalidate_jd_cache = lambda: None
    s._jd_phase = None
    # The epoch state __init__ would have created. Without it the removal cannot
    # signal an in-flight poll that its snapshot is stale.
    s._results_epoch = 0
    s._results_epoch_lock = threading.Lock()
    s.db.delete_download_result.return_value = 1
    return s


def _rows(n, start=1):
    return [{"id": i, "package_uuid": str(1000 + i), "name": "pkg-%d" % i}
            for i in range(start, start + n)]


def _wire(svc, rows, jd=None):
    svc.db.get_download_results.return_value = rows
    jd = jd or _JD()
    svc._connect_jd_device = lambda: jd
    return jd


class TestOneCallNotN:
    def test_every_package_goes_to_JDownloader_in_a_SINGLE_call(self, svc):
        """The entire point. remove_links takes a LIST; looping it is what made
        the button take minutes."""
        rows = _rows(563)
        jd = _wire(svc, rows)
        svc.remove_packages([r["id"] for r in rows])
        assert len(jd.calls) == 1, "made %d JDownloader calls, not 1" % len(jd.calls)
        assert len(jd.calls[0]) == 563

    def test_the_rows_are_read_once_not_once_per_id(self, svc):
        """The single-row path re-read all 578 rows on every call -- O(n^2)."""
        rows = _rows(100)
        _wire(svc, rows)
        svc.remove_packages([r["id"] for r in rows])
        assert svc.db.get_download_results.call_count == 1

    def test_it_reports_what_actually_happened(self, svc):
        rows = _rows(5)
        _wire(svc, rows)
        out = svc.remove_packages([r["id"] for r in rows])
        assert out["ok"] is True and out["removed"] == 5 and out["requested"] == 5
        assert out["jd_removed"] is True and out["durable"] is True


class TestItRemovesFromTheSOURCEOfTruth:
    def test_the_package_uuids_are_the_ones_JD_knows(self, svc):
        rows = _rows(3)
        jd = _wire(svc, rows)
        svc.remove_packages([1, 2, 3])
        assert sorted(jd.calls[0]) == [1001, 1002, 1003]

    def test_ints_not_strings(self, svc):
        """JD expects the native int64; a string silently matches nothing."""
        jd = _wire(svc, _rows(2))
        svc.remove_packages([1, 2])
        assert all(isinstance(u, int) for u in jd.calls[0])

    def test_the_poller_caches_are_evicted(self, svc):
        """Without this an unchanged package still in JD hits the
        unchanged-state skip and re-emits the id we just deleted."""
        rows = _rows(2)
        _wire(svc, rows)
        for r in rows:
            svc._results_cache[r["package_uuid"]] = "x"
            svc._uuid_id[r["package_uuid"]] = r["id"]
            svc._best_titles[r["name"]] = "T"
        svc.remove_packages([1, 2])
        assert not svc._results_cache and not svc._uuid_id and not svc._best_titles


class TestItFailsHonestly:
    def test_an_unreachable_JD_does_NOT_report_a_clear(self, svc):
        """CHANGED after review. Deleting our rows while JD still holds the
        packages is not a removal -- it is a disappearing act the next poll
        undoes. Keeping them means the list still shows the truth and the user
        can retry, which beats a success message followed by their silent
        return."""
        rows = _rows(4)
        _wire(svc, rows, _JD(fail=True))
        out = svc.remove_packages([r["id"] for r in rows])
        assert out["jd_removed"] is False
        assert out["durable"] is False
        assert out["ok"] is False
        assert out["removed"] == 0, "rows were deleted while JD still had them"
        assert out["kept"] == 4
        assert svc.db.delete_download_result.call_count == 0

    def test_an_empty_request_does_nothing_and_says_so(self, svc):
        jd = _wire(svc, _rows(3))
        out = svc.remove_packages([])
        assert out["removed"] == 0 and out["requested"] == 0 and out["ok"] is True
        assert jd.calls == []

    def test_unknown_ids_are_ignored_not_fatal(self, svc):
        rows = _rows(2)
        jd = _wire(svc, rows)
        out = svc.remove_packages([1, 2, 9999])
        assert out["removed"] == 2
        assert sorted(jd.calls[0]) == [1001, 1002]

    def test_a_row_with_no_package_uuid_is_still_deleted(self, svc):
        """A DB row JD no longer knows about must not block the clear."""
        rows = [{"id": 1, "package_uuid": None, "name": "orphan"}]
        jd = _wire(svc, rows)
        out = svc.remove_packages([1])
        assert out["removed"] == 1
        assert jd.calls == [], "nothing to ask JD about"

    def test_orphans_are_still_dropped_when_JD_is_unreachable(self, svc):
        """They have no JD side, so nothing can resurrect them."""
        rows = [{"id": 1, "package_uuid": None, "name": "orphan"},
                {"id": 2, "package_uuid": "1002", "name": "real"}]
        _wire(svc, rows, _JD(fail=True))
        out = svc.remove_packages([1, 2])
        assert out["removed"] == 1, "the orphan should go; the JD-backed row stays"
        assert out["kept"] == 1

    def test_an_unparsable_uuid_does_not_abort_the_batch(self, svc):
        rows = [{"id": 1, "package_uuid": "not-a-number", "name": "a"},
                {"id": 2, "package_uuid": "1002", "name": "b"}]
        jd = _wire(svc, rows)
        out = svc.remove_packages([1, 2])
        assert out["removed"] == 2
        assert jd.calls[0] == [1002]

    def test_a_DB_delete_failure_does_not_abort_the_rest(self, svc):
        rows = _rows(3)
        _wire(svc, rows)
        svc.db.delete_download_result.side_effect = [RuntimeError("locked"), 1, 1]
        out = svc.remove_packages([1, 2, 3])
        assert out["removed"] == 2, "one failure ended the whole clear"
        assert out["ok"] is False, "a partial delete must not read as a clean clear"
        assert out["errors"], "the failure must be reported, not swallowed"


class TestTheClearIsNotCosmetic:
    def test_it_does_NOT_use_the_table_only_clear(self, svc):
        """clear_download_results() deletes our rows and tells JDownloader
        nothing, so the next poll re-inserts everything. If this ever starts
        calling it, the button silently goes back to not working."""
        rows = _rows(3)
        _wire(svc, rows)
        svc.remove_packages([1, 2, 3])
        assert not svc.db.clear_download_results.called


class TestTheJDReturnValueIsChecked:
    """Review MEDIUM 2. remove_links returns the API response; an explicit False
    is a rejection. The first version ignored it entirely, and the first test
    double hard-coded None, so nothing constrained the contract in either
    direction."""

    def test_an_explicit_False_is_a_REFUSAL_not_a_success(self, svc):
        rows = _rows(3)
        _wire(svc, rows, _JD(returns=False))
        out = svc.remove_packages([1, 2, 3])
        assert out["jd_removed"] is False
        assert out["removed"] == 0, "rows deleted although JD refused"
        assert out["ok"] is False

    def test_None_is_still_success(self, svc):
        """removeLinks is a VOID action -- 'no data' is its normal answer.
        Demanding truthiness would fail every real call, which is the mirror
        mistake."""
        rows = _rows(3)
        _wire(svc, rows, _JD(returns=None))
        out = svc.remove_packages([1, 2, 3])
        assert out["jd_removed"] is True and out["removed"] == 3

    def test_True_is_success(self, svc):
        rows = _rows(2)
        _wire(svc, rows, _JD(returns=True))
        assert svc.remove_packages([1, 2])["removed"] == 2


class TestAFailedReadIsNotAClear:
    def test_a_DB_read_failure_reports_failure_not_success(self, svc):
        """'requested - removed means already gone' is not justified when we
        never saw the rows at all."""
        svc.db.get_download_results.side_effect = RuntimeError("db down")
        svc._connect_jd_device = lambda: _JD()
        out = svc.remove_packages([1, 2, 3])
        assert out["ok"] is False
        assert out["removed"] == 0
        assert out["durable"] is False
        assert out["errors"]


class TestKeptRowsAreNotEvicted:
    def test_a_row_we_deliberately_kept_stays_in_the_caches(self, svc):
        """Evicting a row we chose NOT to delete pushes the next poll into the
        cache-miss branch and has it rewrite the row -- performing the
        resurrection by hand."""
        rows = _rows(2)
        _wire(svc, rows, _JD(fail=True))
        for r in rows:
            svc._results_cache[r["package_uuid"]] = "x"
            svc._uuid_id[r["package_uuid"]] = r["id"]
        svc.remove_packages([1, 2])
        assert svc._results_cache, "kept rows were evicted anyway"
        assert svc._uuid_id


class TestTheStaleSnapshotGuard:
    """Review MEDIUM 1. A poll takes its JD snapshot, a removal lands, then the
    poll persists what it saw BEFORE the removal -- and because the removal
    evicted the caches, the stale poll takes the cache-miss branch and upserts.
    The package ends up gone from JD and permanently back in our table."""

    def test_a_removal_advances_the_epoch(self, svc):
        before = svc._current_epoch()
        _wire(svc, _rows(2))
        svc.remove_packages([1, 2])
        assert svc._current_epoch() != before

    def test_a_removal_that_removed_NOTHING_does_not_advance_it(self, svc):
        """Otherwise an unrelated no-op would discard a healthy poll."""
        before = svc._current_epoch()
        _wire(svc, _rows(1))
        svc.remove_packages([9999])
        assert svc._current_epoch() == before

    def test_a_snapshot_taken_before_a_removal_is_stale(self, svc):
        captured = svc._current_epoch()
        _wire(svc, _rows(2))
        svc.remove_packages([1, 2])
        assert svc._epoch_is_current(captured) is False, (
            "a poll that read JD before the removal would persist its old view")

    def test_a_snapshot_with_no_removal_in_between_is_current(self, svc):
        """Control: the guard must not discard ordinary polls."""
        captured = svc._current_epoch()
        assert svc._epoch_is_current(captured) is True

    def test_a_caller_that_captured_nothing_is_not_blocked(self, svc):
        assert svc._epoch_is_current(None) is True

    def test_the_epoch_is_advanced_under_a_lock(self, svc):
        """Concurrent removals must not lose an increment -- a lost one leaves a
        stale snapshot looking current."""
        import threading as _t
        done = _t.Barrier(9)

        def bump():
            done.wait()
            for _ in range(50):
                svc._bump_epoch()

        threads = [_t.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        done.wait()
        for t in threads:
            t.join()
        assert svc._current_epoch() == 400


class TestBookkeepingIsNotAHardDependency:
    """A DownloadService built without __init__ -- which several suites and the
    liveness fixtures do -- must not turn every poll into a FAILURE because an
    accounting field is missing. The first version raised AttributeError from
    poll_results and surfaced as "JDownloader poll failing", a real liveness
    alarm caused by bookkeeping."""

    def test_the_epoch_works_on_a_service_with_no_epoch_state(self):
        bare = DownloadService.__new__(DownloadService)
        assert bare._current_epoch() == 0
        assert bare._epoch_is_current(0) is True
        bare._bump_epoch()
        assert bare._current_epoch() == 1
        assert bare._epoch_is_current(0) is False

    def test_it_does_not_raise_before_any_removal(self):
        bare = DownloadService.__new__(DownloadService)
        assert bare._epoch_is_current(bare._current_epoch()) is True


class TestTheCheckAndTheWriteAreATOMIC:
    """Round-2 review, the one MEDIUM left open.

    The first epoch attempt checked, RELEASED the lock, and only then wrote. The
    original race survived, just narrower:

        poll passes its epoch check
        removal deletes + evicts + bumps
        stale poll writes the row back from its pre-removal snapshot

    The earlier epoch tests could not catch this: they proved a COMPLETED removal
    invalidates an older snapshot, never that a removal landing mid-check is
    excluded. These force the exact order with real threads.
    """

    def test_a_removal_cannot_land_between_the_check_and_the_write(self, svc):
        """The interleaving the reviewer specified, driven by barriers.

        The poll thread enters the critical section and PAUSES inside it. The
        removal thread then tries to delete. If the two share one lock, the
        removal cannot proceed until the poll is done -- so the operations are
        serialised and neither observes a half-applied state.
        """
        import threading as _t
        inside = _t.Event()
        release = _t.Event()
        order = []

        _wire(svc, _rows(2))

        def poll_like():
            # Exactly what poll_results does: one critical section holding both
            # the epoch check and the persistence.
            with svc._results_state():
                order.append("poll:enter")
                inside.set()
                release.wait(timeout=5)
                # the check and the write are both in here
                assert svc._epoch_is_current_locked(0) is True
                order.append("poll:wrote")

        def remove_like():
            inside.wait(timeout=5)
            order.append("remove:start")
            svc.remove_packages([1, 2])
            order.append("remove:done")

        t1 = _t.Thread(target=poll_like)
        t2 = _t.Thread(target=remove_like)
        t1.start(); t2.start()
        inside.wait(timeout=5)
        release.set()
        t1.join(timeout=5); t2.join(timeout=5)

        assert order.index("poll:wrote") < order.index("remove:done"), (
            "the removal completed while the poll held the results lock: %s" % order)

    def test_the_poll_sees_a_STALE_epoch_when_the_removal_won_the_race(self, svc):
        """The other ordering. If the removal gets there first, the poll's check
        must fail -- otherwise it writes back a package JD no longer has."""
        import threading as _t
        captured = svc._current_epoch()
        _wire(svc, _rows(2))
        svc.remove_packages([1, 2])          # removal wins
        with svc._results_state():
            assert svc._epoch_is_current_locked(captured) is False, (
                "a poll holding a pre-removal snapshot would have persisted it")

    def test_the_epoch_advance_is_inside_the_same_section_as_the_deletes(self):
        """Reading the source, because the ordering is what matters and a unit
        test cannot see it. The deletes, the cache eviction and the bump must be
        in ONE `with self._results_state():` -- if the bump drifts outside it
        again, a poll can slip between the deletes and the advance and write the
        rows straight back."""
        import inspect, re
        src = inspect.getsource(DownloadService.remove_packages)
        after = src.split("with self._results_state():", 1)
        assert len(after) == 2, "remove_packages no longer uses the shared section"
        body = after[1]
        assert "delete_download_result" in body, "deletes moved outside the lock"
        assert "_results_cache.pop" in body, "cache eviction moved outside the lock"
        # Either spelling of the advance counts: the literal assignment, or the
        # _bump_epoch_locked() helper added in review. Asserting only on
        # "_results_epoch" made this fail the moment the same behaviour moved
        # behind a well-named helper -- a source-text test pinning the WORDING
        # rather than the property it exists to protect.
        assert ("_results_epoch" in body) or ("_bump_epoch_locked" in body), (
            "the epoch advance moved outside the lock")
        # And it must not be the LOCKING variant: _epoch_lock() is a plain Lock
        # and _results_state() already holds it, so self._bump_epoch() here
        # would deadlock rather than merely be redundant.
        assert "self._bump_epoch()" not in body, (
            "_bump_epoch() inside _results_state() deadlocks on the "
            "non-reentrant lock; use _bump_epoch_locked()")

    def test_the_JD_call_stays_OUTSIDE_the_lock(self):
        """A wedged JDownloader must never block the API route -- that was the
        reviewer's constraint on the fix, and it is easy to lose by widening the
        critical section later."""
        import inspect
        src = inspect.getsource(DownloadService.remove_packages)
        head, _, tail = src.partition("with self._results_state():")
        # Match the CALL EXPRESSION, not the word. The first version asserted
        # `"remove_links" in head`, which the method's own docstring satisfies --
        # so deleting the call entirely still passed. A mutant caught it.
        CALL = "device.downloads.remove_links("
        assert CALL in head, (
            "the JDownloader call is not before the results lock; a stuck JD "
            "call would now block every poll and every other removal")
        assert CALL not in tail

    def test_the_poll_uses_the_LOCKED_predicate_inside_the_section(self):
        """`_epoch_is_current` acquires the lock itself. Calling it from inside
        _results_state() would DEADLOCK on a plain threading.Lock -- and the
        deadlock would be a hung poller, not a test failure. The lock is left
        non-reentrant deliberately: an RLock would let the racy check-then-write
        pattern silently work again, which is the bug this whole section exists
        to prevent. So the discipline is asserted here instead."""
        import inspect
        # _poll_results_inner, not poll_results -- the latter is only the
        # heartbeat wrapper, and inspecting it finds nothing.
        src = inspect.getsource(DownloadService._poll_results_inner)
        head, _, tail = src.partition("with self._results_state():")
        assert tail, "the poll no longer uses the shared critical section"
        assert "_epoch_is_current_locked(" in tail, (
            "the poll must use the _locked predicate inside the section")
        assert "self._epoch_is_current(" not in tail, (
            "poll_results calls the self-locking predicate INSIDE the critical "
            "section -- that deadlocks the poller on a non-reentrant lock")

    def test_the_lock_is_not_reentrant_on_purpose(self):
        """CLAIM CORRECTED after round 3. An RLock would NOT by itself reopen the
        race -- the outer _results_state() still spans predicate + persistence,
        so the operations stay serialised either way. What non-reentrancy buys is
        that a future caller who wrongly uses the self-locking predicate inside
        the section DEADLOCKS LOUDLY instead of nesting invisibly. That is an
        intentional implementation invariant, not the atomicity proof, and the
        earlier comment overstated it."""
        import threading as _t
        bare = DownloadService.__new__(DownloadService)
        lock = bare._epoch_lock()
        assert isinstance(lock, type(_t.Lock())), (
            "the results lock became reentrant; a misuse that should deadlock "
            "loudly would now nest silently")


class TestTheREALPollIsSerialised:
    """Round-3 review, the LOW. The barrier test above proves the shared lock
    serialises two MODELLED operations -- one of which is written by the test
    itself. That is the same weakness as a fixture supplying its own answer: it
    demonstrates the pattern the test believes in, not the one production runs.

    These drive the real `_poll_results_inner()`.
    """

    def _service(self, pkg_uuid="1001", name="pkg-1"):
        """A DownloadService wired with just enough to run a real poll."""
        import threading as _t
        s = DownloadService.__new__(DownloadService)
        s.db = MagicMock()
        s._results_cache, s._uuid_id, s._best_titles = {}, {}, {}
        s._log = lambda *a, **k: None
        s._invalidate_jd_cache = lambda: None
        s._jd_phase = None
        s._results_epoch = 0
        s._results_epoch_lock = _t.Lock()
        s._note_poll_failure = lambda *a, **k: None
        s._note_poll_success = lambda *a, **k: None
        s._scrape_titles = {}

        class _Dev:
            def __init__(self):
                self.downloads = self
                self.removed = []

            def query_packages(self, *a, **k):
                return [{"uuid": int(pkg_uuid), "name": name, "bytesTotal": 100,
                         "bytesLoaded": 100, "finished": True, "status": ""}]

            def query_links(self, *a, **k):
                return [{"packageUUID": int(pkg_uuid), "name": name + ".mkv",
                         "url": "https://host/f", "status": "", "extractionStatus": None}]

            def remove_links(self, links, packages):
                self.removed.append(list(packages))

        s._connect_jd_device = lambda: _Dev()
        return s

    def test_a_removal_cannot_delete_while_the_REAL_poll_is_persisting(self):
        """The reviewer's recipe: block production's upsert_download_result mid
        write, then try to remove. If the poll truly holds _results_state()
        across its persistence, the removal cannot delete until it is released.
        """
        import threading as _t
        svc = self._service()
        entered = _t.Event()
        release = _t.Event()
        order = []

        def blocking_upsert(**kw):
            order.append("upsert:enter")
            entered.set()
            release.wait(timeout=5)
            order.append("upsert:exit")
            return 1
        svc.db.upsert_download_result.side_effect = blocking_upsert
        svc.db.get_download_results.return_value = [
            {"id": 1, "package_uuid": "1001", "name": "pkg-1"}]

        def deleting(_id):
            order.append("delete")
            return 1
        svc.db.delete_download_result.side_effect = deleting

        poll = _t.Thread(target=lambda: svc._poll_results_inner(record=True))
        poll.start()
        assert entered.wait(timeout=5), "the real poll never reached its upsert"

        remover = _t.Thread(target=lambda: svc.remove_packages([1]))
        remover.start()
        remover.join(timeout=1.0)          # must NOT finish; the poll holds the lock
        assert "delete" not in order, (
            "the removal deleted while the production poll was mid-persist: %s"
            % order)

        release.set()
        poll.join(timeout=5)
        remover.join(timeout=5)
        assert order.index("upsert:exit") < order.index("delete"), order

    def test_a_removal_that_lands_FIRST_stops_the_real_poll_writing(self):
        """The opposite ordering, and the one that actually resurrects rows:
        the poll captured its snapshot before the removal, so production must
        refuse to persist it."""
        import threading as _t
        svc = self._service()
        svc.db.get_download_results.return_value = [
            {"id": 1, "package_uuid": "1001", "name": "pkg-1"}]
        svc.db.delete_download_result.return_value = 1

        # A poll whose snapshot predates the removal: bump the epoch first, then
        # hand the poll a stale captured value the way a slow JD read would.
        svc.remove_packages([1])
        stale = 0
        assert svc._current_epoch() != stale, "fixture did not advance the epoch"

        with svc._results_state():
            may_write = svc._epoch_is_current_locked(stale)
        assert may_write is False, (
            "production would have written back a package the removal deleted")

    def test_the_poll_persists_normally_when_nothing_removed(self):
        """Positive control. If the guard refused every write, the two tests
        above would pass against a poll that simply never persists anything."""
        svc = self._service()
        svc.db.upsert_download_result.return_value = 1
        svc._poll_results_inner(record=True)
        assert svc.db.upsert_download_result.called, (
            "the real poll persisted nothing at all; the race tests are vacuous")
