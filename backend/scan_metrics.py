"""Per-scan stage counters and bounded discard diagnostics.

RECORDING ONLY. Nothing in this module suppresses a post, ends a crawl early,
quarantines a URL, or trips a circuit breaker. It observes and counts. A scan
that imports this module must produce exactly the same items it produced before.

Why this exists
---------------
Production scans were fetching ~128 HDEncode detail pages per cycle and keeping
1-4 of them, with both discard paths logging at DEBUG. At INFO the cycle
published as an ordinary success, so a ~98% loss was invisible. Counting the
drop points makes the loss reportable without changing what survives.

Design constraints, from peer review
------------------------------------
* **Stage counters are separate from reason codes.** The counters stay
  meaningful whatever the eventual diagnosis (bad selectors, an interstitial
  page, changed HTML, a parser assumption, one dominant exception). Reason codes
  may be refined later without touching the counter set or the persistence
  shape - hence ``TAXONOMY_VERSION``.
* **``UNKNOWN`` is first class.** An unclassified failure is a visible failure.
  It is never dropped from a ratio and never silently folded into a neighbour.
* **Conservation is checked, not assumed.** If the equations below do not
  balance, the instrumentation is losing events and says so.
* **Samples are bounded and carry no response bodies.**

Deliberately unreachable codes
------------------------------
``MISSING_REQUIRED_TITLE``, ``MISSING_REQUIRED_URL``, ``INVALID_METADATA`` and
``SOURCE_BLOCKED`` are declared but cannot currently fire, and they will report
zero. That is intentional. No corresponding check exists today - a post with no
title becomes an item titled "Unknown" and ships to the UI (pinned by
``tests/test_scanner_service_extended.py::test_result_with_missing_fields_gets_defaults``).
Adding a gate to make these fire would DELETE items that currently survive,
which is a behaviour change and does not belong in a recording-only commit.
Declaring them keeps the taxonomy stable when those checks are added later.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# Bump when reason codes change meaning, so persisted rows stay interpretable.
# Stage counters are versioned separately because they are expected to outlive
# several taxonomy revisions.
TAXONOMY_VERSION = 1

# How many example URLs to retain per reason code. Bounded so a 98% failure
# cycle cannot write 126 rows every hour.
MAX_SAMPLES_PER_REASON = 5


class ScanStage(str, Enum):
    """Where in the pipeline a post was lost."""

    LISTING = "listing"
    DETAIL_FETCH = "detail_fetch"
    DETAIL_PARSE = "detail_parse"
    MEDIA_ITEM_CONSTRUCTION = "media_item_construction"


class DiscardCode(str, Enum):
    """Bounded reason codes. Extend deliberately; never emit a free-form string."""

    # -- detail_fetch ----------------------------------------------------
    DETAIL_CANCELLED = "detail_cancelled"
    DETAIL_TRAFFIC_DENIED = "detail_traffic_denied"
    DETAIL_NO_RESPONSE = "detail_no_response"

    # -- detail_parse ----------------------------------------------------
    DETAIL_NO_FILENAME = "detail_no_filename"
    DETAIL_PARSE_EXCEPTION = "detail_parse_exception"
    # The scraper returned falsy but did not say which of its exits fired.
    DETAIL_EMPTY = "detail_empty"

    # -- media_item_construction -----------------------------------------
    MEDIA_ITEM_EXCEPTION = "media_item_exception"

    # -- declared, currently unreachable (see module docstring) -----------
    MISSING_REQUIRED_TITLE = "missing_required_title"
    MISSING_REQUIRED_URL = "missing_required_url"
    INVALID_METADATA = "invalid_metadata"
    SOURCE_BLOCKED = "source_blocked"

    # -- first-class catch-all -------------------------------------------
    UNKNOWN = "unknown"


STAGE_FOR_CODE: Dict[DiscardCode, ScanStage] = {
    DiscardCode.DETAIL_CANCELLED: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_TRAFFIC_DENIED: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_NO_RESPONSE: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_NO_FILENAME: ScanStage.DETAIL_PARSE,
    DiscardCode.DETAIL_PARSE_EXCEPTION: ScanStage.DETAIL_PARSE,
    DiscardCode.DETAIL_EMPTY: ScanStage.DETAIL_PARSE,
    DiscardCode.MEDIA_ITEM_EXCEPTION: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.MISSING_REQUIRED_TITLE: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.MISSING_REQUIRED_URL: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.INVALID_METADATA: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.SOURCE_BLOCKED: ScanStage.DETAIL_FETCH,
    DiscardCode.UNKNOWN: ScanStage.DETAIL_PARSE,
}

_MESSAGES: Dict[DiscardCode, str] = {
    DiscardCode.DETAIL_CANCELLED: "The scan was stopped before this post's detail page was read.",
    DiscardCode.DETAIL_TRAFFIC_DENIED: "The traffic coordinator declined the detail request.",
    DiscardCode.DETAIL_NO_RESPONSE: "The detail page never returned a usable response after retries.",
    DiscardCode.DETAIL_NO_FILENAME: "The detail page loaded but no Filename field was found; the page layout may have changed.",
    DiscardCode.DETAIL_PARSE_EXCEPTION: "Reading the detail page raised an error.",
    DiscardCode.DETAIL_EMPTY: "The detail scrape returned no data and did not report which step failed.",
    DiscardCode.MEDIA_ITEM_EXCEPTION: "The detail page was read but the release could not be constructed.",
    DiscardCode.MISSING_REQUIRED_TITLE: "The release had no usable title.",
    DiscardCode.MISSING_REQUIRED_URL: "The release had no usable URL.",
    DiscardCode.INVALID_METADATA: "The release metadata failed validation.",
    DiscardCode.SOURCE_BLOCKED: "The source blocked the request.",
    DiscardCode.UNKNOWN: "The post was discarded for a reason the scan could not classify.",
}


def message_for(code: DiscardCode) -> str:
    return _MESSAGES.get(code, _MESSAGES[DiscardCode.UNKNOWN])


@dataclass(frozen=True)
class DiscardSample:
    """One bounded example of a discard.

    Carries no response body. A controlled HTML fixture is captured separately
    and only when explicitly authorized.
    """

    canonical_url: str
    stage: str
    reason_code: str
    source: str = ""
    category: str = ""
    exception_type: Optional[str] = None
    parser_version: int = TAXONOMY_VERSION
    content_fingerprint: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "canonical_url": self.canonical_url,
            "stage": self.stage,
            "reason_code": self.reason_code,
            "source": self.source,
            "category": self.category,
            "exception_type": self.exception_type,
            "parser_version": self.parser_version,
            "content_fingerprint": self.content_fingerprint,
        }


@dataclass
class ScanStageCounters:
    """Non-overlapping per-stage counters for one scan run.

    Thread-safe: detail counters are incremented from worker threads, so every
    mutator takes ``_lock``. ``d[k] += 1`` is three interruptible bytecodes and
    WILL lose updates at the configured thread count.
    """

    listing_pages_requested: int = 0
    listing_pages_succeeded: int = 0
    listing_urls_discovered: int = 0
    listing_urls_new: int = 0
    listing_urls_skipped_cached: int = 0
    listing_blocked_pages: int = 0
    listing_early_stopped: bool = False

    detail_attempted: int = 0
    detail_returned_data: int = 0
    detail_returned_none: int = 0
    detail_raised_exception: int = 0
    detail_cancelled: int = 0

    media_item_created: int = 0
    media_item_construction_failed: int = 0

    reasons: Dict[str, int] = field(default_factory=dict)
    samples: List[DiscardSample] = field(default_factory=list)

    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # -- mutators --------------------------------------------------------

    def note_detail_attempt(self) -> None:
        with self._lock:
            self.detail_attempted += 1

    def note_detail_data(self) -> None:
        with self._lock:
            self.detail_returned_data += 1

    def note_item_created(self) -> None:
        with self._lock:
            self.media_item_created += 1

    def note_discard(
        self,
        code: DiscardCode,
        *,
        url: str = "",
        source: str = "",
        category: str = "",
        exception_type: Optional[str] = None,
        content_fingerprint: Optional[str] = None,
    ) -> None:
        """Record one discarded post.

        Increments exactly one outcome counter so the conservation equations
        hold, tallies the reason, and keeps a bounded sample.
        """
        stage = STAGE_FOR_CODE.get(code, ScanStage.DETAIL_PARSE)
        with self._lock:
            if code is DiscardCode.DETAIL_CANCELLED:
                self.detail_cancelled += 1
            elif code in (
                DiscardCode.DETAIL_PARSE_EXCEPTION,
                DiscardCode.DETAIL_TRAFFIC_DENIED,
            ):
                self.detail_raised_exception += 1
            elif stage is ScanStage.MEDIA_ITEM_CONSTRUCTION:
                self.media_item_construction_failed += 1
            else:
                self.detail_returned_none += 1

            key = code.value
            self.reasons[key] = self.reasons.get(key, 0) + 1
            if self.reasons[key] <= MAX_SAMPLES_PER_REASON and url:
                self.samples.append(
                    DiscardSample(
                        canonical_url=url,
                        stage=stage.value,
                        reason_code=key,
                        source=source,
                        category=category,
                        exception_type=exception_type,
                        content_fingerprint=content_fingerprint,
                    )
                )

    def note_bulk_cancelled(self, count: int) -> None:
        """Book posts abandoned wholesale when a scan is stopped mid-drain.

        Without this the equations can never balance after a Stop press.
        """
        if count <= 0:
            return
        with self._lock:
            self.detail_cancelled += count
            key = DiscardCode.DETAIL_CANCELLED.value
            self.reasons[key] = self.reasons.get(key, 0) + count

    # -- reporting -------------------------------------------------------

    @property
    def detail_success_ratio(self) -> float:
        if self.detail_attempted <= 0:
            return 1.0
        return self.media_item_created / float(self.detail_attempted)

    def conservation_errors(self) -> List[str]:
        """Return human-readable imbalances. Empty means the books balance.

        Deliberately returns rather than raises: a bookkeeping bug must never
        break a scan.
        """
        errors: List[str] = []
        outcomes = (
            self.detail_returned_data
            + self.detail_returned_none
            + self.detail_raised_exception
            + self.detail_cancelled
        )
        if self.detail_attempted != outcomes:
            errors.append(
                "detail_attempted=%d != data+none+exception+cancelled=%d"
                % (self.detail_attempted, outcomes)
            )
        constructed = self.media_item_created + self.media_item_construction_failed
        if self.detail_returned_data != constructed:
            errors.append(
                "detail_returned_data=%d != created+construction_failed=%d"
                % (self.detail_returned_data, constructed)
            )
        discards = (
            self.detail_returned_none
            + self.detail_raised_exception
            + self.detail_cancelled
            + self.media_item_construction_failed
        )
        tallied = sum(self.reasons.values())
        if tallied != discards:
            errors.append(
                "reason tally=%d != discard counters=%d" % (tallied, discards)
            )
        return errors

    def to_dict(self) -> dict:
        return {
            "taxonomy_version": TAXONOMY_VERSION,
            "listing_pages_requested": self.listing_pages_requested,
            "listing_pages_succeeded": self.listing_pages_succeeded,
            "listing_urls_discovered": self.listing_urls_discovered,
            "listing_urls_new": self.listing_urls_new,
            "listing_urls_skipped_cached": self.listing_urls_skipped_cached,
            "listing_blocked_pages": self.listing_blocked_pages,
            "listing_early_stopped": bool(self.listing_early_stopped),
            "detail_attempted": self.detail_attempted,
            "detail_returned_data": self.detail_returned_data,
            "detail_returned_none": self.detail_returned_none,
            "detail_raised_exception": self.detail_raised_exception,
            "detail_cancelled": self.detail_cancelled,
            "media_item_created": self.media_item_created,
            "media_item_construction_failed": self.media_item_construction_failed,
            "detail_success_ratio": round(self.detail_success_ratio, 4),
            "reasons": dict(self.reasons),
            "samples": [s.to_dict() for s in self.samples],
            "conservation_errors": self.conservation_errors(),
        }

    def summary_line(self) -> str:
        """One aggregated line for the phase summary. Never per-post."""
        if not self.reasons:
            return "%d/%d details produced releases" % (
                self.media_item_created,
                self.detail_attempted,
            )
        top = sorted(self.reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        breakdown = ", ".join("%s=%d" % (code, n) for code, n in top)
        return "%d/%d details produced releases; discarded: %s" % (
            self.media_item_created,
            self.detail_attempted,
            breakdown,
        )
