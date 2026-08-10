"""A failed Turnstile is an INTERACTIVE CHALLENGE, not a source throttle.

WHAT WENT WRONG, and why detection has to be a conjunction.

The link reveal on hdencode is gated by an invisible Cloudflare Turnstile
widget. When it fails, ScanHound's reveal control never leaves "Verifying…
Please wait", and every classifier we had said the same thing: the source is
throttling. It was not. The user opened the identical URL in an ordinary browser
and the links appeared in under a second.

That misclassification is not cosmetic. `reveal_verification_stalled` is
retryable with a cooldown, so the queue parked 22 items and set about retrying
them -- into a challenge that had already failed three times in a row and would
fail identically every time, because what it rejects is the browser, not the
request.

MEASURED 2026-08-09 against the live stalled page, and the measurements are why
the fixtures below look the way they do rather than like Cloudflare's
documentation:

  * `input[name="cf-turnstile-response"]` was present in the reveal form with an
    empty value. It is created by `turnstile.render()`, so it exists only when a
    widget really does.
  * There was NO `.cf-turnstile` container and NO `data-sitekey`: the site
    renders the widget programmatically into `#turnstile-container-<hash>`.
  * There was NO queryable challenge <iframe>. The widget runs invisible --
    it builds a frame, fails, tears it down, and retries about every 11
    seconds -- so a DOM read usually lands between attempts.
  * The console carried `[Cloudflare Turnstile] Error: 600010.` repeatedly, and
    all four Turnstile resources loaded HTTP 200 with no `Network.loadingFailed`
    and no CSP on the document. Nothing failed to LOAD; the verdict failed.
  * Six loads later the same URL presented no widget at all and the control read
    "View links", enabled. THE GATE IS INTERMITTENT. That is the single most
    important fact here: neither half of the conjunction is safe alone, because
    each half is true on healthy loads too.

So the tests below check both directions. Evidence without a stalled reveal is
not a failure, and a stalled reveal without evidence is not a challenge.
"""
from __future__ import annotations

import pytest

from backend.download_outcome import turnstile_challenge_evidence
from backend.scrape_outcome import ScrapeCode, _SIGNAL_BEARING_CODES

PAGE_URL = "https://hdencode.org/some-release-2026-2160p-9-0-gb/"
UNLOCK_ACTION = "/some-release-2026-2160p-9-0-gb/#unlocked"


# ── fixtures built from the CAPTURED markup, not from documentation ─────────
def _page(*, body: str = "", head: str = "") -> str:
    return (
        f"<html><head><title>Some.Release.2026.2160p.WEB-DL – 9.0 GB</title>"
        f"{head}</head><body>"
        f'<nav><a href="/tv-shows/">TV Shows</a><a href="/movies/">Movies</a></nav>'
        f"{body}</body></html>"
    )


def _reveal_form(inner: str = "") -> str:
    """The unlock form, with its not-ready submit, as the live page serves it."""
    return (
        f'<form action="{UNLOCK_ACTION}" method="post">'
        f'<input type="submit" value="Verifying… Please wait" disabled>'
        f"{inner}</form>"
    )


#: Transcribed from the live page on 2026-08-09. The nesting -- container div,
#: inner div, hidden input with an empty value -- is the site's, not mine.
RESPONSE_FIELD = (
    '<div id="turnstile-container-16b4ad6f42bf5a881ae9451965133151"><div>'
    '<input type="hidden" name="cf-turnstile-response" '
    'id="cf-chl-widget-c3pvc_response" value=""></div></div>'
)

#: The dormant reference the live page carries in <head> on EVERY release,
#: including ones that reveal links perfectly. Never evidence.
DORMANT_SCRIPT = (
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" '
    "async defer></script>"
)

CHALLENGE_IFRAME = (
    '<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/'
    'h/b/turnstile/f/av0/rch/jn5he/0x4AAA/auto/fbE/new/invisible?lang=auto">'
    "</iframe>"
)

CONSOLE_600 = [{
    "level": "WARNING",
    "message": ('https://challenges.cloudflare.com/turnstile/v0/api.js 0:17636 '
                '"[Cloudflare Turnstile] Error: 600010."'),
}]


def _unlock_target(action: str) -> bool:
    from backend.download_service import _resolves_to_unlock_target
    return _resolves_to_unlock_target(action, PAGE_URL)


# ── the diagnostic path, driven exactly as production drives it ─────────────
class _FakeDecision:
    cooldown_until = "2026-08-09T21:00:00+00:00"


class _FakeCoordinator:
    def __init__(self):
        self.observed = []

    def observe_challenge(self, reason_code="interactive_challenge"):
        self.observed.append(("challenge", reason_code))
        return _FakeDecision()

    def observe_reveal_stall(self, reason_code="reveal_verification_stalled",
                             **_kw):
        self.observed.append(("stall", reason_code))
        return _FakeDecision()

    def observe_reveal_success(self):
        self.observed.append(("success", None))

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
    svc._navigation_console = []
    return svc, coordinator


def _diagnose(svc, *, html, console=(), tier="not-ready"):
    """Run the real _log_page_diagnostics against a driver double."""

    class Driver:
        page_source = html
        title = "Some.Release.2026.2160p.WEB-DL – 9.0 GB"
        current_url = PAGE_URL

        def execute_script(self, *a, **k):
            return None

        def get_log(self, *a, **k):
            return list(console)

    return svc._log_page_diagnostics(
        Driver(), stage="access_control", source_kind="hdencode",
        reveal_tier=tier)


class TestDetectionDiscriminates:
    """Each case names the ONE thing that differs from its neighbour."""

    def test_not_ready_plus_response_field_is_a_challenge(self, service):
        svc, coordinator = service
        d = _diagnose(svc, html=_page(body=_reveal_form(RESPONSE_FIELD)))
        assert d.code is ScrapeCode.INTERACTIVE_CHALLENGE
        assert d.cause_code == "turnstile_challenge_failed"
        assert "turnstile:unsolved-response-field" in d.signals
        # observe_challenge, NOT observe_reveal_stall. The two set different
        # cooldowns for different reasons, and a challenge escalating a
        # throttle dial would be a quiet category error.
        assert ("challenge", "interactive_challenge") in coordinator.observed

    def test_not_ready_plus_challenge_iframe_is_a_challenge(self, service):
        svc, _ = service
        d = _diagnose(svc, html=_page(body=_reveal_form() + CHALLENGE_IFRAME))
        assert d.code is ScrapeCode.INTERACTIVE_CHALLENGE

    def test_not_ready_plus_navigation_scoped_600_console_is_a_challenge(
            self, service):
        svc, _ = service
        d = _diagnose(svc, html=_page(body=_reveal_form()),
                      console=CONSOLE_600)
        assert d.code is ScrapeCode.INTERACTIVE_CHALLENGE
        assert "turnstile:console-600010" in d.signals

    def test_the_600_family_is_matched_not_the_one_observed_code(self):
        """600010 is an observation from one page on one day, not a contract."""
        sibling = [{"message": '"[Cloudflare Turnstile] Error: 600032."'}]
        assert turnstile_challenge_evidence(
            "", console_entries=sibling) == ("turnstile:console-600032",)

    def test_a_dormant_script_tag_alone_is_not_a_challenge(self, service):
        """hdencode ships this in <head> on every release page, healthy ones
        included. Keying on it would call the whole site challenged."""
        svc, coordinator = service
        d = _diagnose(svc, html=_page(head=DORMANT_SCRIPT,
                                      body=_reveal_form()))
        assert d.code is ScrapeCode.REVEAL_VERIFICATION_STALLED
        assert ("stall", "reveal_verification_stalled") in coordinator.observed

    def test_a_tv_shows_link_is_not_a_challenge(self, service):
        """The existing candidate scan matches "show" inside "TV Shows" and
        reports `possible access controls: ["a='TV Shows'"]`. That list is
        observational; it must never reach a classification."""
        svc, _ = service
        html = _page(body=_reveal_form() + '<a href="/tv-shows/">TV Shows</a>')
        d = _diagnose(svc, html=html)
        assert d.code is ScrapeCode.REVEAL_VERIFICATION_STALLED

    def test_no_turnstile_evidence_stays_a_reveal_stall(self, service):
        """The fallback the review requires be RETAINED: not-ready with no
        active challenge evidence is still source-wide and still retryable."""
        svc, _ = service
        d = _diagnose(svc, html=_page(body=_reveal_form()))
        assert d.code is ScrapeCode.REVEAL_VERIFICATION_STALLED
        assert d.retryable is True
        assert d.affected_scope == "source"


class TestTheConjunctionHoldsBothWays:

    def test_evidence_without_a_stalled_reveal_is_not_a_challenge(self, service):
        """MEASURED: six loads presented no widget with the control READY. The
        inverse case is the one that matters -- a widget present on a load that
        goes on to deliver links must not be called a failure."""
        svc, _ = service
        d = _diagnose(svc, html=_page(body=_reveal_form(RESPONSE_FIELD)),
                      console=CONSOLE_600, tier="links-control")
        assert d.code is not ScrapeCode.INTERACTIVE_CHALLENGE

    def test_a_solved_token_is_not_failure_evidence(self):
        """A populated response value is a challenge that SUCCEEDED."""
        solved = RESPONSE_FIELD.replace('value=""', 'value="0.abc123token"')
        assert turnstile_challenge_evidence(
            _page(body=_reveal_form(solved)),
            unlock_target=_unlock_target) == ()

    def test_the_response_field_must_belong_to_the_reveal_form(self):
        """A captcha on the page's comment form says nothing about the reveal.

        wpdiscuz is present on every release page and could plausibly grow its
        own widget; that must not pause the whole source.
        """
        comment_form = (
            '<form action="/wp-comments-post.php">' + RESPONSE_FIELD + "</form>"
        )
        html = _page(body=_reveal_form() + comment_form)
        assert turnstile_challenge_evidence(
            html, unlock_target=_unlock_target) == ()
        # ...and the same field inside the reveal form IS evidence, so the test
        # above is discriminating rather than just failing to parse.
        assert turnstile_challenge_evidence(
            _page(body=_reveal_form(RESPONSE_FIELD)),
            unlock_target=_unlock_target) == ("turnstile:unsolved-response-field",)

    def test_an_input_bound_by_form_attribute_still_counts(self):
        """HTML allows association by `form="<id>"` as well as by nesting.
        Only the nested shape was observed, so the other is asserted rather
        than assumed."""
        html = _page(
            body=(f'<form id="unlockform" action="{UNLOCK_ACTION}">'
                  f'<input type="submit" value="Verifying… Please wait"></form>'
                  '<input type="hidden" name="cf-turnstile-response" '
                  'form="unlockform" value="">')
        )
        assert turnstile_challenge_evidence(
            html, unlock_target=_unlock_target) == ("turnstile:unsolved-response-field",)


class TestNavigationScoping:

    def test_a_previous_pages_error_must_not_classify_this_one(self, service):
        """The browser session is persistent and shared. Without the reset at
        navigation start, ONE stalled release would go on explaining every
        release grabbed after it."""
        svc, _ = service

        class Driver:
            page_source = _page(body=_reveal_form())
            title = "t"
            current_url = PAGE_URL
            drained = 0

            def execute_script(self, *a, **k):
                return None

            def get_log(self, *a, **k):
                # Chrome's log is a QUEUE: the reset drains it, so the stale
                # entry is served once and never again.
                Driver.drained += 1
                return list(CONSOLE_600) if Driver.drained == 1 else []

        driver = Driver()
        svc._reset_console_log(driver)          # what _navigate does
        d = svc._log_page_diagnostics(
            driver, stage="access_control", source_kind="hdencode",
            reveal_tier="not-ready")
        assert d.code is ScrapeCode.REVEAL_VERIFICATION_STALLED

    def test_within_one_navigation_earlier_entries_are_retained(self, service):
        """A single navigation is diagnosed more than once and each drain
        empties Chrome's queue. Replacing rather than appending would discard
        the first Turnstile error, which arrives ~2s after load."""
        svc, _ = service

        class Driver:
            page_source = _page(body=_reveal_form())
            title = "t"
            current_url = PAGE_URL
            drained = 0

            def execute_script(self, *a, **k):
                return None

            def get_log(self, *a, **k):
                Driver.drained += 1
                return list(CONSOLE_600) if Driver.drained == 1 else []

        driver = Driver()
        svc._reset_console_log(driver)
        Driver.drained = 0                      # the error arrives AFTER reset
        svc._drain_console_log(driver)          # the pre-click read
        d = svc._log_page_diagnostics(
            driver, stage="access_control", source_kind="hdencode",
            reveal_tier="not-ready")
        assert d.code is ScrapeCode.INTERACTIVE_CHALLENGE


class TestTheEvidenceSurvives:

    def test_challenge_signals_are_persisted_not_only_logged(self):
        """The 2026-07-31 burst of 64 failures became permanently unexplainable
        because its signals lived only in a log that had rotated by the time
        anyone looked. Evidence that decides an operator's next action has to
        reach the row."""
        assert ScrapeCode.INTERACTIVE_CHALLENGE in _SIGNAL_BEARING_CODES

    def test_the_mechanism_reaches_the_row_through_cause_code(self, service):
        svc, _ = service
        d = _diagnose(svc, html=_page(body=_reveal_form(RESPONSE_FIELD)))
        assert d.to_dict()["cause_code"] == "turnstile_challenge_failed"
        assert "turnstile:" in d.persisted_message


class TestTheMessageDoesNotPromiseTheImpossible:

    def test_it_does_not_tell_the_user_to_verify_something(self):
        """There is no path from a click in ScanHound to the Xvfb Chromium
        session being challenged. VerificationRetries.svelte offers Retry now /
        Retry all / Remove and nothing else."""
        from backend.download_outcome import _FAILURE_TITLES

        from backend.scrape_outcome import ScrapeDiagnostic

        title = _FAILURE_TITLES[ScrapeCode.INTERACTIVE_CHALLENGE.value]
        assert title == "Manual attention required"
        message = ScrapeDiagnostic(
            ScrapeCode.INTERACTIVE_CHALLENGE).public_message.lower()
        assert "did not complete" in message
        for promise in ("verify you", "complete the verification",
                        "solve", "click the challenge"):
            assert promise not in message
