"""A stalled reveal control is a RETRYABLE reveal-path failure, not a changed layout.

ITS CAUSE IS NOT ESTABLISHED. Corrected 2026-08-08 on peer review round 10.

This module opened by asserting "a source throttle" and closed the evidence section
with "The source is rate-limiting." I had already renamed the class and stripped the
claim from the user-facing message in the same PR -- and left the framing here intact,
which is where the assumption was actually coming from. Every test in the file
inherited it, and that is how `assert "rate-limit" in message` came to look like a
reasonable requirement.

WHAT THE OBSERVATIONS BELOW ESTABLISH: the reveal control was present, the page shape
was unchanged, and the widget had not finished when OUR 60-second window expired. So
this is not a layout change and it is worth retrying.

WHAT THEY DO NOT ESTABLISH: that the source is rate-limiting us. Every 60s figure is
RIGHT-CENSORED -- measurement stops at the ceiling, so a widget that would have
finished at 62s is indistinguishable from one that never finishes. And ScanHound reuses
a persistent Chromium profile, so source-side limiting is indistinguishable here from
browser/session state. See docs/reviews/peer-rounds/reveal-stall-root-cause.md.

PRODUCTION EVIDENCE, 2026-08-06. HDEncode gates each link reveal behind a
client-side countdown. The submit reads "Verifying... Please wait" until it
clears, then swaps to "View links". Observed sequence from the app log:

    13:21  tier=links-control  elapsed=0.8s  found=True   -> DELIVERED
    13:30  tier=links-control  elapsed=0.2s  found=True   -> DELIVERED
    13:40  tier=links-control  elapsed=0.1s  found=True   -> DELIVERED
    13:50  tier=links-control  elapsed=0.1s  found=True   -> clicked, NO links
    14:01  tier=not-ready      elapsed=60.4s found=False
    14:11  tier=not-ready      elapsed=60.5s found=False
    14:21  tier=not-ready      elapsed=60.2s found=False
    14:31  tier=not-ready      elapsed=60.5s found=False
    14:41  tier=not-ready      elapsed=60.0s found=False

Three reveals succeed, then the door shuts and stays shut. The page shape is
identical throughout -- 6 forms, the same #unlocked action, 92-94 links -- so
nothing about the layout changed. Something changed STATE; what owns that state is
not known from this data.

WHY THIS MATTERED MORE THAN ONE ITEM. The stall was classified LAYOUT_CHANGED,
which is retryable=False, carries no cooldown, and never notifies the traffic
coordinator. So the batch never paused, the queue kept marching at its spacing,
and every remaining item hit the same closed door and became PERMANENTLY
terminal. 78 items accumulated that way, with automated_retry_count 0 on every
one. One throttle event burned the whole queue.
"""
import pytest

from backend.scrape_outcome import ScrapeCode, ScrapeDiagnostic


class _FakeDecision:
    cooldown_until = "2026-08-06T20:00:00+00:00"


class _FakeCoordinator:
    """Records what the production path tells it."""

    def __init__(self):
        self.observed = []

    def observe_challenge(self, reason_code="interactive_challenge"):
        self.observed.append(reason_code)
        return _FakeDecision()

    def observe_reveal_stall(self, reason_code="reveal_verification_stalled",
                             **kwargs):
        # The production path calls THIS, not observe_challenge, since the
        # reveal cooldown became configurable and escalating. When this method
        # was missing the AttributeError was swallowed by a broad handler and
        # surfaced as SCRAPE_EXCEPTION -- a silent degradation worth knowing
        # about: a real coordinator API mismatch would look like a generic
        # scrape failure rather than a wiring error.
        self.observed.append(reason_code)
        return _FakeDecision()

    def observe_reveal_success(self):
        self.observed.append("reveal_success")

    def snapshot(self):
        return {"blocked": False}


@pytest.fixture
def service(monkeypatch):
    from backend import download_service as ds

    coordinator = _FakeCoordinator()
    monkeypatch.setattr(ds, "get_hdencode_coordinator", lambda: coordinator)
    svc = object.__new__(ds.DownloadService)
    svc._log = lambda *a, **k: None
    svc._last_cf_mitigated = None
    return svc, coordinator, ds


def _diagnose(svc, ds, *, tier, html="<html><body><form></form></body></html>"):
    """Run the real _log_page_diagnostics against a driver double."""

    class Driver:
        page_source = html
        title = "Some.Release.2026.2160p.WEB-DL – 9.0 GB"
        current_url = "https://hdencode.org/some-release-2026-2160p-9-0-gb/"

        def execute_script(self, *a, **k):
            return None

        def get_log(self, *a, **k):
            return []

    return svc._log_page_diagnostics(
        Driver(), stage="access_control", source_kind="hdencode",
        reveal_tier=tier)


class TestStalledVerifyIsRetryableNotALayoutChange:
    """What a stalled reveal IS, stated only as far as the evidence goes.

    RENAMED 2026-08-08 from TestStalledVerifyIsAThrottle. The old name asserted the
    conclusion in its title, and every test inside inherited that framing -- which is
    how `assert "rate-limit" in message` came to look like a reasonable thing to
    require. A stalled reveal is established to be: retryable, not a layout change,
    cooldown-bearing, and reported to the coordinator. Whether the SOURCE is
    throttling us is not established; see
    docs/reviews/peer-rounds/reveal-stall-root-cause.md.

    NOTE the tests below still assert source-wide scope and coordinator notification,
    because that is the behaviour as built. Round 9 argues the scope is too broad --
    a per-item reveal failure should trip a reveal circuit breaker, not a global
    source breaker -- but that is a design change, not a claim correction, so it is
    left for its own round rather than quietly altered here.
    """

    def test_it_is_not_reported_as_a_layout_change(self, service):
        svc, _, ds = service
        d = _diagnose(svc, ds, tier="not-ready")
        assert d.code == ScrapeCode.REVEAL_VERIFICATION_STALLED
        assert d.code != ScrapeCode.LAYOUT_CHANGED

    def test_it_stays_retryable(self, service):
        """The single most important assertion in this file.

        retryable=False is why 78 items became permanent. The release is fine;
        the source was busy.
        """
        svc, _, ds = service
        assert _diagnose(svc, ds, tier="not-ready").retryable is True

    def test_it_carries_a_cooldown(self, service):
        svc, _, ds = service
        assert _diagnose(svc, ds, tier="not-ready").cooldown_until is not None

    def test_it_tells_the_traffic_coordinator(self, service):
        """Without this the backoff system never learns the reveal path stalled.

        Said as "the source is refusing" until 2026-08-08 -- the same unproven
        attribution in miniature. What the coordinator is told is that a reveal did
        not complete; what that implies about the source is the open question.
        """
        svc, coordinator, ds = service
        _diagnose(svc, ds, tier="not-ready")
        assert coordinator.observed == ["reveal_verification_stalled"]

    def test_it_pauses_the_source_not_just_the_item(self, service):
        """affected_scope='source' is what stops the rest of the queue being
        burned by the same closed door."""
        svc, _, ds = service
        d = _diagnose(svc, ds, tier="not-ready")
        assert d.affected_scope == "source"
        assert d.retry_mode == "after_cooldown"
        assert d.action_code == "wait_for_cooldown"

    def test_the_message_does_not_blame_the_release(self, service):
        """Assert the property this test is NAMED for, not a mechanism.

        CORRECTED 2026-08-08. This asserted `"rate-limit" in message` -- so a test of
        mine REQUIRED the causal claim that peer review round 9 showed is unmeasured,
        and it failed the moment I removed it. Fourth time one of my tests has
        protected the thing it should have caught.

        The proxy was also wrong on its own terms: naming a mechanism is not what
        makes a message not blame the release. What must hold is that the user is
        told the release is fine and will be retried, and is not told the page
        changed. Both of those ARE established.
        """
        svc, _, ds = service
        message = _diagnose(svc, ds, tier="not-ready").public_message.lower()
        assert "nothing is wrong with this release" in message
        assert "retry" in message
        assert "layout" not in message
        # And it must not assert a cause nobody has measured.
        for unproven in ("rate-limit", "rate limiting", "throttl"):
            assert unproven not in message, (
                f"the message asserts {unproven!r}; every 60s stall observation is "
                "right-censored and cannot establish it")

    def test_the_tier_is_recorded_in_the_signals(self, service):
        svc, _, ds = service
        assert "reveal-tier:not-ready" in _diagnose(svc, ds, tier="not-ready").signals


class TestARealLayoutChangeIsUnaffected:
    """The discrimination. If these regress, the fix has become a blanket
    excuse that would hide an actual site change."""

    @pytest.mark.parametrize("tier", [None, "none", "ambiguous",
                                      "destination-rejected"])
    def test_other_tiers_remain_terminal_layout_changes(self, service, tier):
        svc, coordinator, ds = service
        d = _diagnose(svc, ds, tier=tier)
        assert d.code == ScrapeCode.LAYOUT_CHANGED
        assert d.retryable is False
        assert d.cooldown_until is None
        assert coordinator.observed == [], (
            "a real layout change must not trigger a source cooldown")


class TestTheQueueActuallyPausesTheSource:
    """The CONSUMER, not the diagnostic.

    Setting affected_scope and retry_mode on the diagnostic changes nothing by
    itself. download_queue routes an outcome by is_source_wide_denial(), which
    requires the reason_code to be in _SOURCE_WIDE_REASONS. Without membership
    there, every field above is decorative: the outcome still reaches _fail and
    the item still becomes terminal.

    I nearly shipped exactly that. These tests exist because checking the
    producer and not the consumer is the mistake I keep making.
    """

    def _outcome(self, svc, ds, tier="not-ready"):
        return _diagnose(svc, ds, tier=tier).to_dict()

    def test_a_stalled_verify_is_a_source_wide_denial(self, service):
        from backend.download_outcome import is_source_wide_denial
        svc, _, ds = service
        outcome = self._outcome(svc, ds)
        assert is_source_wide_denial(outcome) is True, (
            "the queue would send this to _fail and mark it terminal")

    def test_the_reason_code_is_in_the_source_wide_set(self, service):
        from backend.download_outcome import _SOURCE_WIDE_REASONS
        from backend.scrape_outcome import ScrapeCode
        assert ScrapeCode.REVEAL_VERIFICATION_STALLED.value in _SOURCE_WIDE_REASONS

    @pytest.mark.parametrize("tier", [None, "none", "ambiguous",
                                      "destination-rejected"])
    def test_a_real_layout_change_is_NOT_source_wide(self, service, tier):
        """It must still fail the single item rather than pausing the source."""
        from backend.download_outcome import is_source_wide_denial
        svc, _, ds = service
        assert is_source_wide_denial(self._outcome(svc, ds, tier=tier)) is False

    def test_the_deferred_result_carries_the_cooldown(self, service):
        """The batch pause needs cooldown_until to know when to auto-resume."""
        from backend.download_outcome import deferred_result
        svc, _, ds = service
        result = deferred_result(self._outcome(svc, ds), title="T",
                                 url="https://hdencode.org/x-1-gb")
        assert result["deferred"] is True
        assert result.get("cooldown_until") or result.get("until")
