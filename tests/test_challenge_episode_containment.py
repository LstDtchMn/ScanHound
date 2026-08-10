"""One challenge is answered ONCE, not re-presented to every item behind it.

THE DEFECT THIS FILE EXISTS FOR, stated as a fact about the code rather than a
worry. Before this change:

    decide(ItemFacts(state="verification_required",
                     queue_reason="interactive_challenge",
                     cooldown_until=<expired>), shared) == AUTHORISED

because DEFERRED_STATES contains "verification_required" and RECOGNISED_REASONS
contains "interactive_challenge", so the row fell through to the time checks and
an expired timer authorised it. And `action_for(AUTHORISED)` is ACTION_NONE, so
every operator tool reported "nothing to do; the scheduler will pick it up"
about a row whose entire meaning is that the scheduler cannot.

A clock released a hold that only a person can release. That was survivable only
while nothing produced these rows in volume -- and classifying Turnstile
correctly is exactly what produces them in volume. Fixing the label without
fixing this would have taken 22 items parked behind one closed door and fed all
22 back through it automatically.

WHAT AN EPISODE IS. The unit a human answers. One challenge presented to one
item is one problem, not 22, so the triggering row and every sibling parked
behind it are held on ONE fact and released on ONE affirmative success -- this
browser session completing a real reveal on this source. Not a timer. Not a
human succeeding in a different browser, which proves the challenge is passable
and nothing at all about the session that failed it.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from backend import download_queue as dq_module
from backend.database import DatabaseManager
from backend.download_outcome import _SOURCE_WIDE_REASONS
from backend.download_queue import DownloadQueueError, DownloadQueueService
from backend.queue_recovery_policy import (
    ACTION_ATTENTION_REQUIRED, ACTION_FOR, ALL_DECISIONS, AUTHORISED,
    ACTION_ADVICE, ItemFacts, NEEDS_HUMAN, SharedFacts, VERIFICATION_HOLD,
    action_for, decide,
)
from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic

START = datetime(2026, 8, 9, 18, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)
        return self.now


class ScriptedDownloads:
    """Records every URL it is asked for.

    "No sibling was attempted" is asserted as the ABSENCE OF A CALL, not the
    absence of a state change: a state change can be undone by something else,
    but a transport attempt that never happened cannot be faked away.
    """

    def __init__(self):
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

    def __getattr__(self, name):
        def _noop(*_a, **_k):
            return None
        return _noop


def challenge_outcome(clock, *, minutes=60):
    """The real INTERACTIVE_CHALLENGE shape a failed Turnstile now produces.

    Built from ScrapeDiagnostic rather than a literal dict so it cannot keep
    passing after the production contract moves underneath it.
    """
    return ScrapeDiagnostic(
        code=ScrapeCode.INTERACTIVE_CHALLENGE,
        retryable=False,
        affects_source_health=True,
        transport_attempted=True,
        affected_scope="source",
        retry_mode="manual_verification",
        cause_code="turnstile_challenge_failed",
        cooldown_until=(clock.now + timedelta(minutes=minutes)).isoformat(),
        action_code="verification_required",
        stage="verification",
        signals=("reveal-tier:not-ready", "turnstile:unsolved-response-field"),
    ).to_dict()


def stall_outcome(clock, *, minutes=60):
    """A reveal stall with NO challenge evidence: the retained fallback."""
    return ScrapeDiagnostic(
        code=ScrapeCode.REVEAL_VERIFICATION_STALLED,
        retryable=True,
        affects_source_health=True,
        transport_attempted=False,
        affected_scope="source",
        retry_mode="after_cooldown",
        cooldown_until=(clock.now + timedelta(minutes=minutes)).isoformat(),
        action_code="wait",
        stage="link_retrieval",
    ).to_dict()


def success_outcome():
    return {"success": True, "method": "jdownloader", "link_count": 1,
            "message": "sent", "reason_code": "", "stage": "download",
            "retryable": False, "retry_mode": "none",
            "transport_attempted": True, "source_progress": True,
            "affected_scope": "item", "action_code": "", "signals": []}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(dq_module, "_utcnow", clock)
    db = DatabaseManager(str(tmp_path / "episode.db"))
    downloads = ScriptedDownloads()
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


def _schedule(service, count=3, *, auto_resume=True):
    items = [{"url": f"https://hdencode.org/release-{i}-2160p/",
              "title": f"Release {i}", "media_type": "movie"}
             for i in range(count)]
    return service.schedule_batch(items, interval_minutes=0, mode="immediate",
                                  auto_resume_after_cooldown=auto_resume)


def _states(service, batch_uuid):
    batch = service.get_batch(batch_uuid)
    return {row["title"]: row["state"] for row in batch["items"]}


class TestTheDriverIsHonest:

    def test_the_driver_matches_the_real_worker_body(self):
        src = inspect.getsource(DownloadQueueService._worker)
        for call in ("_maybe_auto_resume()", "_claim_due()", "_execute("):
            assert call in src, (
                f"_worker no longer calls {call}; the _tick driver in this file "
                "is stale and every assertion below tests a sequence that "
                "production does not run")


# ── the pure policy ─────────────────────────────────────────────────────────
class TestATimerNeverReleasesAHumanHold:

    def test_the_exact_facts_that_used_to_return_authorised(self):
        """The blocking defect, reproduced verbatim.

        Expired cooldown, everything else healthy. This returned AUTHORISED.
        """
        expired = START - timedelta(hours=2)
        item = ItemFacts(state="verification_required",
                         cooldown_until=expired,
                         queue_reason="interactive_challenge")
        shared = SharedFacts(cooldown_until=expired, auto_resume_enabled=True,
                             attempts_used=0)
        assert decide(item, shared, START) == VERIFICATION_HOLD

    def test_no_amount_of_waiting_changes_it(self):
        item = ItemFacts(state="verification_required",
                         cooldown_until=START - timedelta(days=365),
                         queue_reason="interactive_challenge")
        shared = SharedFacts(cooldown_until=None, auto_resume_enabled=True,
                             attempts_used=0)
        for years in (1, 10, 100):
            later = START + timedelta(days=365 * years)
            assert decide(item, shared, later) == VERIFICATION_HOLD

    def test_siblings_are_held_by_the_open_episode(self):
        """A sibling never met the challenge -- it was parked behind one -- so
        its own reason is the ordinary source_deferred. The episode is what
        stops its expired cooldown from authorising it."""
        expired = START - timedelta(hours=2)
        sibling = ItemFacts(state="waiting_source", cooldown_until=expired,
                            queue_reason="source_deferred")
        healthy = SharedFacts(cooldown_until=expired, auto_resume_enabled=True,
                              attempts_used=0)
        held = SharedFacts(cooldown_until=expired, auto_resume_enabled=True,
                           attempts_used=0, challenge_open=True)
        # The control: WITHOUT the episode this row is authorised, so the
        # assertion below is discriminating rather than vacuous.
        assert decide(sibling, healthy, START) == AUTHORISED
        assert decide(sibling, held, START) == VERIFICATION_HOLD

    def test_the_hold_outranks_a_disabled_batch(self):
        """DISABLED advises "turn auto-resume back on; the items are fine".
        That is wrong here and would send an operator to a setting that cannot
        help, so ordering matters, not just membership."""
        item = ItemFacts(state="verification_required", cooldown_until=None,
                         queue_reason="interactive_challenge")
        shared = SharedFacts(cooldown_until=None, auto_resume_enabled=False,
                             attempts_used=99)
        assert decide(item, shared, START) == VERIFICATION_HOLD

    def test_safety_still_outranks_it(self):
        """An unknown outcome stays first. A challenge row whose previous
        attempt may already have delivered must still be adjudicated, because
        the duplicate-delivery risk outranks everything."""
        from backend.queue_recovery_policy import SAFETY_HOLD
        item = ItemFacts(state="verification_required", cooldown_until=None,
                         queue_reason="interactive_challenge",
                         last_reason_code="operation_timeout_unknown")
        shared = SharedFacts(cooldown_until=None, auto_resume_enabled=True,
                             attempts_used=0)
        assert decide(item, shared, START) == SAFETY_HOLD


class TestTheAdviceIsHonest:

    def test_the_decision_needs_a_human_and_has_an_action(self):
        assert VERIFICATION_HOLD in ALL_DECISIONS
        assert VERIFICATION_HOLD in NEEDS_HUMAN
        assert action_for(VERIFICATION_HOLD) == ACTION_ATTENTION_REQUIRED

    def test_every_decision_still_has_an_action(self):
        """The invariant that stops a new decision reaching an operator tool
        before anyone decided what a human should do about it."""
        assert set(ACTION_FOR) == ALL_DECISIONS

    def test_action_for_still_fails_closed(self):
        with pytest.raises(KeyError):
            action_for("some_future_decision")

    def test_the_advice_does_not_promise_that_retrying_completes_it(self):
        advice = ACTION_ADVICE[ACTION_ATTENTION_REQUIRED].lower()
        assert "manual attention required" in advice
        assert "probe" in advice
        # It must NOT be the manual-resume promise, which says a plain resume
        # finishes the job.
        assert "safe to resume" not in advice

    def test_it_is_not_the_same_action_as_a_plain_manual_resume(self):
        from backend.queue_recovery_policy import ACTION_MANUAL_RESUME
        assert ACTION_ATTENTION_REQUIRED != ACTION_MANUAL_RESUME


# ── the consumer boundary ───────────────────────────────────────────────────
class TestSourceWideContainmentIsRetained:

    def test_the_challenge_code_still_routes_to_the_source_pause(self):
        """Membership here is what sends the outcome to _pause_for_source
        instead of _fail. Losing it is how 78 items became permanent."""
        assert ScrapeCode.INTERACTIVE_CHALLENGE.value in _SOURCE_WIDE_REASONS

    def test_the_trigger_is_held_and_the_siblings_are_parked_not_failed(
            self, rig):
        clock, _db, downloads, service = rig
        batch = _schedule(service, 3)
        downloads.queue(challenge_outcome(clock))
        _tick(service)

        states = _states(service, batch["batch_uuid"])
        assert list(states.values()).count("verification_required") == 1
        assert list(states.values()).count("waiting_source") == 2
        assert "failed" not in states.values()
        assert len(downloads.calls) == 1


class TestTheLoadBearingNegativeControl:
    """22 items, one challenge. The whole point of the change, in one test."""

    def test_one_challenge_does_not_become_twentytwo(self, rig):
        clock, _db, downloads, service = rig
        batch = _schedule(service, 22, auto_resume=True)

        # The FIRST item meets an active Turnstile. Exactly one outcome is
        # scripted, so any further attempt raises inside ScriptedDownloads --
        # the assertion is enforced by the harness, not only by a count.
        downloads.queue(challenge_outcome(clock, minutes=60))
        _tick(service)

        states = _states(service, batch["batch_uuid"])
        assert list(states.values()).count("verification_required") == 1, (
            "exactly one row met the challenge")
        assert list(states.values()).count("waiting_source") == 21, (
            "the other 21 are held behind it")
        assert list(states.values()).count("failed") == 0, (
            "no sibling became a permanent failure")
        assert len(downloads.calls) == 1, (
            "21 siblings were never attempted")

        # NOW ADVANCE PAST EVERY COOLDOWN. The item cooldowns, the batch
        # cooldown, and any plausible escalation of either.
        for _ in range(12):
            clock.advance(hours=24)
            _tick(service)

        assert len(downloads.calls) == 1, (
            "a timer released a hold that only a person can release: "
            f"{downloads.calls[1:]}")
        states = _states(service, batch["batch_uuid"])
        assert list(states.values()).count("verification_required") == 1
        assert list(states.values()).count("waiting_source") == 21
        assert list(states.values()).count("failed") == 0

    def test_the_rig_really_can_promote_when_nothing_is_held(self, rig):
        """THE POSITIVE CONTROL, without which the test above proves nothing.

        Same rig, same clock advance, a reveal stall carrying NO challenge
        evidence. If this did not resume, the negative control above would pass
        on a rig that simply never promotes anything.
        """
        clock, _db, downloads, service = rig
        _schedule(service, 22, auto_resume=True)
        downloads.queue(stall_outcome(clock, minutes=60))
        _tick(service)
        assert len(downloads.calls) == 1

        clock.advance(hours=2)
        for _ in range(3):
            downloads.queue(success_outcome())
        _tick(service)
        assert len(downloads.calls) > 1, (
            "the rig never promotes anything, so the negative control is "
            "vacuous")


class TestTheProbeIsOneItem:

    def test_retry_all_is_refused_while_an_episode_is_open(self, rig):
        clock, _db, downloads, service = rig
        batch = _schedule(service, 22)
        downloads.queue(challenge_outcome(clock))
        _tick(service)

        with pytest.raises(DownloadQueueError) as raised:
            service.resume_batch(batch["batch_uuid"])
        assert "single item" in str(raised.value)
        with pytest.raises(DownloadQueueError):
            service.retry_ready()
        assert len(downloads.calls) == 1

    def test_an_explicit_probe_promotes_exactly_one_item(self, rig):
        clock, _db, downloads, service = rig
        batch = _schedule(service, 22)
        downloads.queue(challenge_outcome(clock))
        _tick(service)

        trigger = next(row["item_uuid"]
                       for row in service.get_batch(batch["batch_uuid"])["items"]
                       if row["state"] == "verification_required")
        service.retry_item(trigger)

        ready = [row["state"]
                 for row in service.get_batch(batch["batch_uuid"])["items"]]
        assert ready.count("ready") == 1, "the probe is one item, not 22"
        assert ready.count("waiting_source") == 21


class TestReleaseTakesAnAffirmativeSuccess:

    def test_a_real_delivery_closes_the_episode_and_frees_the_siblings(
            self, rig):
        clock, _db, downloads, service = rig
        batch = _schedule(service, 5)
        downloads.queue(challenge_outcome(clock))
        _tick(service)

        trigger = next(row["item_uuid"]
                       for row in service.get_batch(batch["batch_uuid"])["items"]
                       if row["state"] == "verification_required")
        service.retry_item(trigger)
        downloads.queue(success_outcome())
        _tick(service)                       # the probe succeeds

        # The episode is closed, so the siblings become eligible again -- and
        # they are released by the ordinary auto-resume path, which spaces them
        # by the batch interval rather than firing all four at once.
        for _ in range(4):
            downloads.queue(success_outcome())
        clock.advance(hours=2)
        _tick(service)
        assert len(downloads.calls) > 2, "siblings stayed held after a success"

    def test_a_duplicate_completion_does_not_close_it(self, rig):
        """download_item returns success with method='duplicate' BEFORE
        scraping when the release was already grabbed. That never contacted the
        source, so it is not evidence the challenge lifted."""
        clock, _db, downloads, service = rig
        batch = _schedule(service, 5)
        downloads.queue(challenge_outcome(clock))
        _tick(service)

        trigger = next(row["item_uuid"]
                       for row in service.get_batch(batch["batch_uuid"])["items"]
                       if row["state"] == "verification_required")
        service.retry_item(trigger)
        duplicate = {**success_outcome(), "method": "duplicate",
                     "source_progress": False}
        downloads.queue(duplicate)
        _tick(service)

        with pytest.raises(DownloadQueueError):
            service.resume_batch(batch["batch_uuid"])
