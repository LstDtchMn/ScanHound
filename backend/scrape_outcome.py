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
    BROWSER_LAUNCH_FAILED = "browser_launch_failed"
    BROWSER_NETWORK_ERROR = "browser_network_error"
    BROWSER_NAVIGATION_FAILED = "browser_navigation_failed"
    INTERACTIVE_CHALLENGE = "interactive_challenge"
    LAYOUT_CHANGED = "layout_changed"
    REQUESTED_HOST_MISSING = "requested_host_missing"
    NO_FILE_HOST_LINKS = "no_file_host_links"
    SCRAPE_EXCEPTION = "scrape_exception"


_MESSAGES = {
    ScrapeCode.SOURCE_DISABLED: "HDEncode is disabled in Settings; no request was made.",
    ScrapeCode.BROWSER_LAUNCH_FAILED: "The browser could not start. Check the Chromium/Xvfb service and profile locks.",
    ScrapeCode.BROWSER_NETWORK_ERROR: "Chromium could not reach the source because of a browser network or DNS error.",
    ScrapeCode.BROWSER_NAVIGATION_FAILED: "The browser failed while navigating to the source page.",
    ScrapeCode.INTERACTIVE_CHALLENGE: "The source presented an interactive verification challenge that did not clear.",
    ScrapeCode.LAYOUT_CHANGED: "The expected link-reveal control was not found; the page layout may have changed.",
    ScrapeCode.REQUESTED_HOST_MISSING: "The page loaded, but it does not contain links for the requested file host.",
    ScrapeCode.NO_FILE_HOST_LINKS: "The page loaded, but no supported file-host links were found.",
    ScrapeCode.SCRAPE_EXCEPTION: "The link scrape failed before download links could be retrieved.",
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

    @property
    def public_message(self) -> str:
        """Stable user-facing text that never includes raw exception details."""
        return _MESSAGES[self.code]

    @property
    def message(self) -> str:
        """Internal diagnostic text; may include a logged exception detail."""
        return self.detail or self.public_message

    def to_dict(self) -> dict:
        return {
            "reason_code": self.code.value,
            "message": self.public_message,
            "retryable": self.retryable,
            "affects_source_health": self.affects_source_health,
            "transport": self.transport,
            "status_code": self.status_code,
            "signals": list(self.signals),
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
