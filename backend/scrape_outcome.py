"""Transport-agnostic scrape outcomes with list compatibility.

``ScrapedLinks`` deliberately subclasses ``list`` so existing callers, mocks,
and tests continue to work while a structured diagnostic can travel with an
empty result. This avoids the mechanically breaking tuple migration that would
otherwise make every caller update atomically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence


class ScrapeCode(str, Enum):
    SOURCE_DISABLED = "source_disabled"
    SOURCE_TEMPORARILY_BLOCKED = "source_temporarily_blocked"
    BROWSER_LAUNCH_FAILED = "browser_launch_failed"
    BROWSER_NETWORK_ERROR = "browser_network_error"
    BROWSER_NAVIGATION_FAILED = "browser_navigation_failed"
    INTERACTIVE_CHALLENGE = "interactive_challenge"
    # POSITIVE structural evidence that the reveal contract differs from what
    # ScanHound expects: a links-labelled submit whose destination no longer
    # matches the unlock endpoint ("destination-rejected"), or several otherwise
    # valid reveal controls ("ambiguous"). Both are things we DID observe.
    LAYOUT_CHANGED = "layout_changed"
    # No qualifying reveal control was proven, and no challenge was recognised.
    # DELIBERATELY NEUTRAL (peer review 2026-08-12): this used to be reported as
    # LAYOUT_CHANGED, which picks one hypothesis without evidence. Absence is
    # equally consistent with a page-specific restriction, a login/session or
    # region gate, a pulled release, an error page, an unrecognised block, an
    # alternate release template -- or a real layout change. Splitting it from
    # LAYOUT_CHANGED is what makes a genuine source-wide regression detectable by
    # aggregation, since one page can no longer impersonate it.
    REVEAL_CONTROL_ABSENT = "reveal_control_absent"
    # The reveal control existed but never left its "Verifying... Please wait"
    # state. Distinct from SOURCE_TEMPORARILY_BLOCKED, whose message says no
    # request was made -- here the page was fetched and the control was found.
    REVEAL_VERIFICATION_STALLED = "reveal_verification_stalled"
    REQUESTED_HOST_MISSING = "requested_host_missing"
    NO_FILE_HOST_LINKS = "no_file_host_links"
    SCRAPE_EXCEPTION = "scrape_exception"
    # The URL IS the download link -- a direct file host, not a source page. There
    # is nothing to scrape, so this is not a failure: download_item hands the URL
    # straight to the downloader. Added because dispatch previously fell through
    # to the HDEncode reveal-page path for these.
    DIRECT_LINK_NO_SOURCE_PAGE = "direct_link_no_source_page"
    # A host we have no scraper for. Naming it is the point: it used to be handled
    # by the HDEncode implementation, which reported a reveal-control failure for
    # a site that was never HDEncode.
    UNSUPPORTED_SOURCE = "unsupported_source"


_MESSAGES = {
    ScrapeCode.SOURCE_DISABLED: "HDEncode is disabled in Settings; no request was made.",
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED: (
        "HDEncode is temporarily paused to protect the source; no request was made."
    ),
    ScrapeCode.BROWSER_LAUNCH_FAILED: "The browser could not start. Check the Chromium/Xvfb service and profile locks.",
    ScrapeCode.BROWSER_NETWORK_ERROR: "Chromium could not reach the source because of a browser network or DNS error.",
    ScrapeCode.BROWSER_NAVIGATION_FAILED: "The browser failed while navigating to the source page.",
    ScrapeCode.INTERACTIVE_CHALLENGE: "The source presented an interactive verification challenge that did not clear.",
    # Asserting "the layout changed" overstates what this code knows. It fires
    # only after the challenge branch above has already ruled out a cf-mitigated
    # header, captcha frames and challenge markers -- so what remains is "the
    # control was not there and it was not a recognised challenge", which is a
    # login/region gate, an error page, an unrecognised block, OR a real layout
    # change. download_service's own log at the access_control call site says
    # exactly that: "may be a Cloudflare wall, login gate, or changed layout".
    # The user-facing text said only the last of those, which reads as "the
    # scraper is broken" when 64 such failures appeared in bursts on 2026-07-30,
    # 07-31 and 08-04 and then stopped on their own -- a real layout change does
    # not heal twice.
    ScrapeCode.LAYOUT_CHANGED: (
        "The link-reveal control was not on the page, and this was not a "
        "recognised verification challenge. The page may be a login or region "
        "gate, an unrecognised block, an error page, or a changed layout."
    ),
    # Says only what was observed: the control was not found. It deliberately
    # does NOT name a cause, because absence does not identify one.
    ScrapeCode.REVEAL_CONTROL_ABSENT: (
        "The link-reveal control was not found on this page, and no verification "
        "challenge was recognised. This page alone does not show why -- it may be "
        "a login or region gate, a pulled release, an error page, an unrecognised "
        "block, or a different page template."
    ),
    ScrapeCode.REQUESTED_HOST_MISSING: "The page loaded, but it does not contain links for the requested file host.",
    ScrapeCode.NO_FILE_HOST_LINKS: "The page loaded, but no supported file-host links were found.",
    # NO CAUSAL CLAIM. This used to say the stall is what the source "does when it
    # is rate-limiting", which was never measured -- see the long note at the
    # emission site in download_service. The reason CODE
    # (`reveal_verification_stalled`) was always neutral and correct; only this
    # user-facing prose overstated. What Jesse needs to know is unchanged and true:
    # the release is fine and it will be retried.
    ScrapeCode.REVEAL_VERIFICATION_STALLED: (
        "The source did not finish showing the download links in time. The item is "
        "queued to retry after a cooldown; nothing is wrong with this release."
    ),
    ScrapeCode.SCRAPE_EXCEPTION: "The link scrape failed before download links could be retrieved.",
    ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE: (
        "This link is already a file-host download link, so there is no source "
        "page to read. It is being sent to the downloader as-is."
    ),
    ScrapeCode.UNSUPPORTED_SOURCE: (
        "This website is not one ScanHound knows how to read download links from."
    ),
}


# The codes whose cause is genuinely ambiguous, so the collected signals are
# worth persisting alongside the message. Every other code names its own cause.
#
# INTERACTIVE_CHALLENGE was added 2026-08-09: which challenge fired and which
# evidence proved it (an iframe, the cf-turnstile-response field, a console
# 600* error) is exactly what the next investigation needs, and the Turnstile
# root cause could be named only because the app log happened not to have
# rotated yet. The mechanism also travels in cause_code, but the evidence list
# has no other durable home.
_SIGNAL_BEARING_CODES = frozenset({
    ScrapeCode.LAYOUT_CHANGED,
    # The reveal tier IS the evidence here, and absence has no other cause to
    # name, so the signals are the whole diagnostic.
    ScrapeCode.REVEAL_CONTROL_ABSENT,
    ScrapeCode.REVEAL_VERIFICATION_STALLED,
    ScrapeCode.NO_FILE_HOST_LINKS,
    ScrapeCode.REQUESTED_HOST_MISSING,
    ScrapeCode.INTERACTIVE_CHALLENGE,
})


#: DID REACHING THIS VERDICT REQUIRE CONTACTING THE SOURCE?
#:
#: This is a property of the CODE, not of the call site. It used to be an
#: optional constructor argument defaulting to None, and nine of the fourteen
#: construction sites in download_service never passed it -- including
#: LAYOUT_CHANGED and REVEAL_CONTROL_ABSENT, which are decided only AFTER the
#: page has been fetched and inspected. `bool(None)` is False, so those two were
#: persisted as "never contacted the source", and scraper_drift_report() counts
#: only transport_attempted = 1. The drift detector therefore shipped unable to
#: see a single real structural failure, while its own tests passed because they
#: build attempt rows directly instead of going through this producer.
#:
#: Declared here so the question is answered once per code and cannot be
#: forgotten at a call site. `test_every_scrape_code_declares_transport` fails if
#: a new code is added without an entry -- an omission must be a build error, not
#: a silent False. A call site may still pass transport_attempted explicitly to
#: override, for the genuinely conditional cases.
#:
#: The consumers this feeds, and why a wrong answer is costly in BOTH directions:
#:   * F4 source pacing -- True spends the source's capacity lane
#:   * _scope_is_earned  -- only True rows are evidence about the source
#:   * queue_source_observations -- source liveness
#:   * scraper_drift_report -- only True rows can indicate template drift
#: False on something that did contact the source hides evidence; True on
#: something that did not manufactures it.
_TRANSPORT_BY_CODE = {
    # Refused locally, before any request. The message says so in as many words.
    ScrapeCode.SOURCE_DISABLED: False,
    ScrapeCode.SOURCE_TEMPORARILY_BLOCKED: False,
    # The browser never started, so nothing left the machine.
    ScrapeCode.BROWSER_LAUNCH_FAILED: False,
    # A network or DNS error IS an attempt to reach the source. It failed, but
    # "we tried and could not connect" is not the same as "we never asked", and
    # only the first is evidence about the source.
    ScrapeCode.BROWSER_NETWORK_ERROR: True,
    ScrapeCode.BROWSER_NAVIGATION_FAILED: True,
    # Everything below is decided by looking at a page we fetched.
    ScrapeCode.INTERACTIVE_CHALLENGE: True,
    ScrapeCode.LAYOUT_CHANGED: True,
    ScrapeCode.REVEAL_CONTROL_ABSENT: True,
    ScrapeCode.REVEAL_VERIFICATION_STALLED: True,
    ScrapeCode.REQUESTED_HOST_MISSING: True,
    ScrapeCode.NO_FILE_HOST_LINKS: True,
    # AMBIGUOUS BY NATURE, resolved CONSERVATIVELY as True. The exception may
    # have been raised before or after navigation; we cannot tell from the code
    # alone. True is the safer error: it spends the source's pacing lane for one
    # interval, where False would let a failure that may well have hammered the
    # source go unpaced. Pass transport_attempted=False explicitly at a site that
    # KNOWS nothing was sent.
    ScrapeCode.SCRAPE_EXCEPTION: True,
    # The URL is already a file-host link, so there is no source page and this is
    # not a failure at all -- nothing was fetched from a source.
    ScrapeCode.DIRECT_LINK_NO_SOURCE_PAGE: False,
    # Declined before dispatch: we have no scraper for this host.
    ScrapeCode.UNSUPPORTED_SOURCE: False,
}


@dataclass(frozen=True)
class ScrapeDiagnostic:
    code: ScrapeCode
    transport: str = "selenium"
    retryable: bool = False
    affects_source_health: bool = False
    status_code: Optional[int] = None
    signals: Sequence[str] = field(default_factory=tuple)
    detail: str = ""
    stage: str = "link_retrieval"
    cause_code: Optional[str] = None
    cooldown_until: Optional[str] = None
    transport_attempted: Optional[bool] = None
    affected_scope: str = "item"
    retry_mode: str = "none"
    action_code: Optional[str] = None
    deferred: bool = False
    health_owner: str = "outcome_recorder"

    @property
    def public_message(self) -> str:
        """Stable user-facing text that never includes raw exception details."""
        return _MESSAGES[self.code]

    @property
    def effective_transport_attempted(self) -> bool:
        """Whether this outcome means the source was actually contacted.

        An explicit constructor value wins; otherwise the answer comes from the
        code itself (_TRANSPORT_BY_CODE). Never returns None: every consumer
        coerces with bool(), so a None here silently becomes False, and False is
        an ASSERTION that nothing was sent -- which for a post-navigation code is
        simply untrue. Making this total is what stops the drift detector, the
        scope classifier and the pacing gate from being fed a fabricated fact.
        """
        if self.transport_attempted is not None:
            return bool(self.transport_attempted)
        return _TRANSPORT_BY_CODE[self.code]

    @property
    def persisted_message(self) -> str:
        """User-facing text, plus the signals when the code is ambiguous.

        WHY THIS EXISTS. The diagnostic collects rich signals
        (access_control_present/absent, cf-mitigated:challenge, keyword
        presence) and to_dict() serialises them -- but nothing persisted them.
        download_queue_items stores only last_reason_code, last_cause_code and
        last_message. So when the 2026-07-31 burst of 64 layout_changed failures
        was investigated on 08-06, the signals existed only in the app log, which
        had rotated. The failures could be counted and dated but NOT explained,
        and that root cause is now permanently unknowable.

        Only the ambiguous codes carry signals; the rest already name their own
        cause, so appending would be noise. Raw exception detail is still never
        included -- signals are a fixed vocabulary produced by our own code.
        """
        base = _MESSAGES[self.code]
        if self.code not in _SIGNAL_BEARING_CODES:
            return base
        seen = [str(s) for s in self.signals if s is not None]
        if not seen:
            return base
        return f"{base} [signals: {', '.join(seen)}]"

    @property
    def message(self) -> str:
        """Internal diagnostic text; may include a logged exception detail."""
        return self.detail or self.public_message

    def to_dict(self) -> dict:
        return {
            "reason_code": self.code.value,
            "cause_code": self.cause_code,
            # persisted_message, not public_message: this dict is what reaches
            # download_queue_items.last_message, and for the ambiguous codes the
            # signals are the only durable record of what the page actually was.
            "message": self.persisted_message,
            "retryable": self.retryable,
            "retry_mode": self.retry_mode,
            "cooldown_until": self.cooldown_until,
            # An explicit value at the call site still wins; None means "you did
            # not say", and the answer is a property of the code, not an
            # accidental False. See _TRANSPORT_BY_CODE.
            "transport_attempted": self.effective_transport_attempted,
            "affected_scope": self.affected_scope,
            "action_code": self.action_code,
            "deferred": self.deferred,
            "stage": self.stage,
            "affects_source_health": self.affects_source_health,
            "transport": self.transport,
            "status_code": self.status_code,
            "signals": [str(value) for value in self.signals if value is not None],
        }


class ScrapedLinks(list[str]):
    """A normal list of URLs with an optional structured failure diagnostic."""

    def __init__(
        self,
        links: Iterable[str] = (),
        *,
        diagnostic: Optional[ScrapeDiagnostic] = None,
    ) -> None:
        super().__init__(links)
        self.diagnostic = diagnostic
