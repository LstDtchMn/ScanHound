"""The whole throttle -> pause -> cooldown -> resume loop, as one sequence.

WHY THIS FILE EXISTS. The 2026-08-06 peer review said the reveal-throttle tests
"test the links individually, not this lifecycle", and required an end-to-end test
with the real queue worker, a fake clock and a deterministic download service. Its
eleven numbered assertions are implemented below, each labelled.

The review was right in a way the session then proved three times over. Every
defect that night lived in a GAP between pieces that each tested fine alone:

  * the diagnostic carried affected_scope='source' and retry_mode='after_cooldown'
    and eleven tests passed -- but download_queue routes on set membership, so the
    fields were decorative and the item would still have failed terminally;
  * five batches were armed to retry, and would have been skipped in silence,
    because the resume path matches item and batch cooldown timestamps as strings
    and only one side had been changed;
  * the auto-resume config default turned out to govern two of the three grab
    paths, because the third always sends its own value.

None of those are visible from inside a single unit. They are only visible when
the sequence runs.

HOW THE WORKER IS DRIVEN. `_tick` below is the body of the real `_worker` loop
verbatim, minus the sleep:

    self._maybe_auto_resume()
    item = self._claim_due()
    if item is not None: self._execute(item)

Nothing about policy is reimplemented -- claiming, cooldown, pausing and resuming
are all the production code. `test_the_driver_matches_the_real_worker_body` guards
against this driver drifting away from `_worker` if the loop is ever changed,
because a stale driver would let this whole file pass while testing a sequence
production no longer runs.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from backend import download_queue as dq_module
from backend.database import DatabaseManager
from backend.download_outcome import _SOURCE_WIDE_REASONS
from backend.download_queue import DownloadQueueService
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic

START = datetime(2026, 8, 6, 18, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """One clock for the queue module, advanced explicitly by each test."""

    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)
        return self.now


class ScriptedDownloads:
    """A deterministic download service: one scripted outcome per call.

    Records every URL it is asked for, which is how "no sibling was attempted
    during the cooldown" is asserted -- absence of a call, not absence of a state
    change, because a state change could be undone by something else.
    """

    def __init__(self, clock):
        self.clock = clock
        self.script = []
        self.calls = []

    def queue(self, outcome):
        self.script.append(outcome)

    def download_item(self, *, url, title, **_kw):
        self.calls.append(url)
        if not self.script:
            raise AssertionError(
                f"the worker attempted {url!r} with nothing scripted -- an "
                "unexpected attempt is itself a finding")
        return self.script.pop(0)

    # The queue calls these on some paths; keep them inert and observable.
    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


def stall_outcome(clock, *, minutes=60):
    """The real REVEAL_VERIFICATION_STALLED shape, built from the real class.

    Built from ScrapeDiagnostic rather than a hand-written dict on purpose: a
    literal would keep passing after the production contract changed, which is the
    stale-shape trap this codebase has hit before.
    """
    until = (clock.now + timedelta(minutes=minutes)).isoformat()
    return ScrapeDiagnostic(
        code=ScrapeCode.REVEAL_VERIFICATION_STALLED,
        retryable=True,
        affects_source_health=True,
        transport_attempted=False,
        affected_scope="source",
        retry_mode="after_cooldown",
        cooldown_until=until,
        action_code="wait",
        stage="link_retrieval",
    ).to_dict()


def layout_outcome():
    """A genuine layout change: terminal, no cooldown, item-scoped."""
    return ScrapeDiagnostic(
        code=ScrapeCode.LAYOUT_CHANGED,
        retryable=False,
        transport_attempted=False,
        affected_scope="item",
        retry_mode="none",
        stage="link_retrieval",
    ).to_dict()


def success_outcome():
    return {"success": True, "method": "jdownloader", "link_count": 1,
            "message": "sent", "reason_code": "", "stage": "download",
            "retryable": False, "retry_mode": "none",
            "transport_attempted": True, "affected_scope": "item",
            "action_code": "", "signals": []}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dq_module, "_utcnow", clock)
    db = DatabaseManager(str(tmp_path / "lifecycle.db"))
    downloads = ScriptedDownloads(clock)
    service = DownloadQueueService({}, db, downloads, poll_seconds=0.01)
    yield clock, db, downloads, service
    db.close()


def _tick(service):
    """The real _worker body, minus the sleep."""
    service._maybe_auto_resume()
    item = service._claim_due()
    if item is None:
        return None
    service._execute(item)
    return item


def _schedule(service, count=3, *, auto_resume=False):
    items = [{"url": f"https://hdencode.org/release-{i}-2160p/",
              "title": f"Release {i}", "media_type": "movie"}
             for i in range(count)]
    return service.schedule_batch(items, interval_minutes=0, mode="immediate",
                                  auto_resume_after_cooldown=auto_resume)


def _states(service, batch_uuid):
    batch = service.get_batch(batch_uuid)
    return batch, {row["title"]: row["state"] for row in batch["items"]}


class TestTheDriverIsHonest:

    def test_the_driver_matches_the_real_worker_body(self):
        """If _worker changes, this file would silently test a dead sequence."""
        src = inspect.getsource(DownloadQueueService._worker)
        for call in ("_maybe_auto_resume()", "_claim_due()", "_execute("):
            assert call in src, (
                f"_worker no longer calls {call}; the _tick driver in this file "
                "is stale and every assertion below is testing a sequence that "
                "production does not run")


class TestTheThrottleLifecycle:
    """Reviewer assertions 1-5, in one continuous sequence."""

    def test_a_stall_defers_the_batch_and_holds_the_siblings(self, rig):
        clock, db, downloads, service = rig
        batch = _schedule(service, 3)
        uuid = batch["batch_uuid"]

        # (1) the first item returns REVEAL_VERIFICATION_STALLED
        downloads.queue(stall_outcome(clock))
        first = _tick(service)
        assert first is not None, "the worker claimed nothing; the rig is broken"
        assert len(downloads.calls) == 1

        current, states = _states(service, uuid)
        by_uuid = {r["item_uuid"]: r for r in current["items"]}
        attempted = by_uuid[first["item_uuid"]]

        # (2) the item is scheduled/deferred, NOT failed
        assert attempted["state"] != "failed", (
            "THE ORIGINAL BUG. Before the fix this became a permanent failure and "
            f"79 grabs were burned this way. state={attempted['state']!r}")
        assert attempted["state"] in ("waiting_source", "scheduled", "ready"), (
            attempted["state"])

        # (3) pending siblings receive the cooldown
        siblings = [r for r in current["items"]
                    if r["item_uuid"] != first["item_uuid"]]
        assert siblings, "need siblings to test sibling behaviour"
        assert all(r["cooldown_until"] for r in siblings), (
            "a source-wide denial must defer the whole batch, not just the item "
            "that happened to hit it: " + repr(
                [(r["title"], r["cooldown_until"]) for r in siblings]))

        # (4) the batch becomes paused
        assert current["state"] == "paused_source", current["state"]

        # (5) no sibling is attempted during the cooldown
        before = len(downloads.calls)
        for _ in range(4):
            assert _tick(service) is None, (
                "the worker claimed an item while the source was in cooldown")
        assert len(downloads.calls) == before, (
            "the worker called the download service during the cooldown; "
            "hammering a rate-limiting source is what caused the incident")


class TestAutoResumePolicy:
    """Reviewer assertions 6-10."""

    def test_with_auto_resume_disabled_nothing_restarts(self, rig):
        """(6) The default. This is why 58 grabs sat for a week."""
        clock, db, downloads, service = rig
        batch = _schedule(service, 3, auto_resume=False)
        uuid = batch["batch_uuid"]
        downloads.queue(stall_outcome(clock, minutes=60))
        _tick(service)
        assert _states(service, uuid)[0]["state"] == "paused_source"

        clock.advance(hours=6)          # long past the cooldown
        for _ in range(5):
            _tick(service)
        current, _ = _states(service, uuid)
        assert current["state"] == "paused_source", (
            "with auto-resume disabled the batch must stay parked; an automatic "
            "restart here would contradict the documented default")
        assert len(downloads.calls) == 1, (
            "no further attempt should have been made")

    def test_with_auto_resume_enabled_the_clock_releases_it_once(self, rig):
        """(7) one resume, (8) the deferred item is reclaimed, (9) the batch
        continues after a success."""
        clock, db, downloads, service = rig
        batch = _schedule(service, 3, auto_resume=True)
        uuid = batch["batch_uuid"]
        downloads.queue(stall_outcome(clock, minutes=60))
        _tick(service)
        assert _states(service, uuid)[0]["state"] == "paused_source"

        # Before the cooldown expires: still nothing.
        clock.advance(minutes=30)
        assert _tick(service) is None, "resumed before the cooldown expired"

        # (7) advance past it: exactly one resume
        clock.advance(minutes=45)
        downloads.queue(success_outcome())
        reclaimed = _tick(service)
        assert reclaimed is not None, (
            "the cooldown expired with auto-resume enabled and the batch did NOT "
            "resume. This is the silent-skip failure mode: _maybe_auto_resume "
            "requires an item whose cooldown_until equals the batch's exactly, "
            "and if that match breaks it skips with no diagnostic.")
        current, _ = _states(service, uuid)
        assert current["auto_resume_used"] == 1, (
            "the resume must be recorded as spent; without it the one-shot policy "
            "cannot be enforced")

        # (8)+(9) work continues
        assert len(downloads.calls) == 2
        downloads.queue(success_outcome())
        downloads.queue(success_outcome())
        for _ in range(6):
            _tick(service)
        _, states = _states(service, uuid)
        assert list(states.values()).count("completed") >= 2, states

    def test_the_second_stall_proves_the_one_shot_policy(self, rig):
        """(10) The retry is one per batch for its LIFETIME, not per pause.

        Documented here because it was described to the user as one-per-pause and
        it is not: the resume query requires auto_resume_used = 0 and the column
        only ever increments.
        """
        clock, db, downloads, service = rig
        batch = _schedule(service, 3, auto_resume=True)
        uuid = batch["batch_uuid"]

        downloads.queue(stall_outcome(clock, minutes=60))
        _tick(service)
        clock.advance(minutes=61)
        downloads.queue(stall_outcome(clock, minutes=60))   # stalls AGAIN
        assert _tick(service) is not None, "the first resume should have fired"
        current, _ = _states(service, uuid)
        assert current["auto_resume_used"] == 1
        assert current["state"] == "paused_source", (
            "the second stall must pause the batch again")

        clock.advance(hours=4)
        calls_before = len(downloads.calls)
        for _ in range(6):
            _tick(service)
        current, _ = _states(service, uuid)
        assert current["state"] == "paused_source", (
            "the one-shot policy must hold: a batch that has spent its automatic "
            "resume never resumes itself again, however long you wait")
        assert len(downloads.calls) == calls_before, (
            "no attempt may follow a spent resume")


class TestLayoutChangeStaysTerminal:

    def test_a_real_layout_change_still_fails_terminally(self, rig):
        """(11) The fix must not have made everything retryable.

        If LAYOUT_CHANGED had become deferrable, the throttle fix would have
        traded a false-terminal bug for a false-retry bug -- retrying a genuinely
        broken page forever.
        """
        clock, db, downloads, service = rig
        batch = _schedule(service, 2, auto_resume=True)
        uuid = batch["batch_uuid"]

        # TIE THE FIXTURE TO PRODUCTION. Found by mutation while writing this
        # file: adding LAYOUT_CHANGED to _SOURCE_WIDE_REASONS -- a genuine
        # false-retry regression -- failed NOTHING, because
        # is_source_wide_denial() needs set membership AND
        # affected_scope='source', and layout_outcome() hard-codes 'item'. So the
        # test was asserting the behaviour of its own fixture rather than of
        # production. This line closes that: the terminal verdict below only means
        # something while production keeps this reason out of the source-wide set.
        assert ScrapeCode.LAYOUT_CHANGED.value not in _SOURCE_WIDE_REASONS, (
            "LAYOUT_CHANGED is now routed as a source-wide denial, so a genuinely "
            "broken page would defer and retry forever instead of failing once. "
            "That trades the original false-terminal bug for a false-retry bug")

        downloads.queue(layout_outcome())
        first = _tick(service)
        current, _ = _states(service, uuid)
        by_uuid = {r["item_uuid"]: r for r in current["items"]}
        attempted = by_uuid[first["item_uuid"]]
        assert attempted["state"] == "failed", (
            f"a genuine layout change must stay terminal, got "
            f"{attempted['state']!r}")
        assert current["state"] != "paused_source", (
            "an item-scoped failure must not pause the whole batch")

        # POSITIVE CONTROL for this test: the sibling keeps working, proving the
        # failure was scoped to the item and not to the source.
        downloads.queue(success_outcome())
        assert _tick(service) is not None, (
            "the sibling must still be attempted after an item-scoped failure")
