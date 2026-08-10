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
    LAYOUT_CHANGED = "layout_changed"
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
    # PROMISES NOTHING, and specifically not that the user can finish the
    # verification. They cannot: the challenge is presented to a headless
    # Chromium running under Xvfb inside the container, and the only controls
    # the UI offers are Retry now / Retry all / Remove. There is no path from a
    # click in ScanHound to that browser session. Saying "verification required"
    # implied there was one, and sent the reader looking for a button that has
    # never existed.
    ScrapeCode.INTERACTIVE_CHALLENGE: (
        "Automated verification did not complete. The source presented a "
        "verification challenge that ScanHound's browser could not finish, so "
        "the links were never shown. Waiting will not clear this and a retry "
        "presents the same challenge again."
    ),
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
_SIGNAL_BEARING_CODES = frozenset({
    ScrapeCode.LAYOUT_CHANGED,
    ScrapeCode.REVEAL_VERIFICATION_STALLED,
    ScrapeCode.NO_FILE_HOST_LINKS,
    ScrapeCode.REQUESTED_HOST_MISSING,
    # ADDED 2026-08-09. This code does name its own cause at the level of "a
    # challenge", which is why it was excluded -- but "a challenge" turned out
    # not to be specific enough to act on. Which challenge, presented by what,
    # and proven by which marker are the facts that decide whether an operator
    # is looking at a Turnstile that failed to execute, a Cloudflare
    # interstitial, or a captcha on some unrelated form. Without them the
    # signals live only in the app log, and the 2026-07-31 burst of 64
    # layout_changed failures is the standing proof of what that costs: by the
    # time anyone looked, the log had rotated and the cause became permanently
    # unknowable.
    ScrapeCode.INTERACTIVE_CHALLENGE,
})


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
            "transport_attempted": self.transport_attempted,
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
