"""Per-scan stage counters and bounded discard diagnostics.

RECORDING ONLY. Nothing here suppresses a post, ends a crawl early, quarantines
a URL, or trips a circuit breaker. A scan that imports this module must produce
exactly the same items it produced before. Every public method is no-throw: a
bookkeeping bug must never reach scan control flow.

Why this exists
---------------
Production scans fetched ~128 HDEncode detail pages per cycle and kept 1-4, with
both discard paths logging at DEBUG. At INFO the cycle published as an ordinary
success, so a ~98% loss was invisible.

Counter model
-------------
Scheduling, execution and HTTP cost are three different quantities. Collapsing
them inflates the denominator on a stopped scan and makes any ratio - and any
future circuit-breaker threshold - depend on thread scheduling and Stop timing::

    detail_scheduled = detail_started + detail_cancelled_before_start
    detail_started   = detail_returned_data + detail_returned_none
                     + detail_raised_exception + detail_cancelled_after_start
    detail_returned_data = media_item_created + media_item_construction_failed

``detail_http_requests`` sits outside those equations on purpose: the scraper
retries up to three times per started post, so requests and posts are not
interchangeable and request economics cannot be derived from post counts.

Stage is independent of reason
------------------------------
The caller supplies the factual stage at the event site. ``STAGE_FOR_CODE`` is a
default and a test oracle, never the sole source of truth - otherwise refining a
reason code retroactively re-stages new events, and a factual ``UNKNOWN`` at
fetch or construction would be mis-filed as a parse failure.

One terminal event per post
---------------------------
Instrumentation spans two layers: the detail scraper knows which of its seven
exits fired; the scanner knows about scheduling, cancellation and construction.
If both book an outcome for one post, conservation breaks. ``PostOutcome`` is a
per-post ticket that accepts exactly one terminal event and ignores the rest, so
the outer layer can safely fall back to a generic reason only when the inner
layer recorded nothing.

Deliberately unreachable codes
------------------------------
``MISSING_REQUIRED_TITLE``, ``MISSING_REQUIRED_URL``, ``INVALID_METADATA`` and
``SOURCE_BLOCKED`` are declared but cannot fire and will report zero. No
corresponding check exists - a post with no title becomes an item titled
"Unknown" and ships to the UI (pinned by
``tests/test_scanner_service_extended.py::test_result_with_missing_fields_gets_defaults``).
Adding a gate to make them fire would DELETE items that currently survive.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# Meaning of reason codes and the stage-default map. Bump when those change.
# Deliberately NOT the parser version: that describes the detail-parsing
# contract whose change should invalidate quarantine and force a retry.
TAXONOMY_VERSION = 1

MAX_SAMPLES_PER_REASON = 5


class ScanStage(str, Enum):
    LISTING = "listing"
    DETAIL_FETCH = "detail_fetch"
    DETAIL_PARSE = "detail_parse"
    MEDIA_ITEM_CONSTRUCTION = "media_item_construction"


class DiscardCode(str, Enum):
    """Bounded reason codes. Each names a FACTUAL BRANCH, not a diagnosis."""

    # -- the seven detail_scraper exits ----------------------------------
    DETAIL_CANCELLED_BEFORE_REQUEST = "detail_cancelled_before_request"
    DETAIL_CANCELLED_IN_COORDINATOR = "detail_cancelled_in_coordinator"
    DETAIL_TRAFFIC_DENIED = "detail_traffic_denied"
    DETAIL_RETRY_SLEEP_CANCELLED = "detail_retry_sleep_cancelled"
    DETAIL_NO_USABLE_RESPONSE = "detail_no_usable_response"
    DETAIL_NO_FILENAME = "detail_no_filename"
    DETAIL_PARSE_EXCEPTION = "detail_parse_exception"

    # Outer fallback: the scraper returned falsy without booking a reason.
    DETAIL_EMPTY = "detail_empty"

    # -- scanner-owned ----------------------------------------------------
    DETAIL_CANCELLED_BEFORE_START = "detail_cancelled_before_start"
    DETAIL_CANCELLED_AFTER_START = "detail_cancelled_after_start"
    MEDIA_ITEM_EXCEPTION = "media_item_exception"

    # -- declared, currently unreachable (see module docstring) -----------
    MISSING_REQUIRED_TITLE = "missing_required_title"
    MISSING_REQUIRED_URL = "missing_required_url"
    INVALID_METADATA = "invalid_metadata"
    SOURCE_BLOCKED = "source_blocked"

    # -- first-class catch-all --------------------------------------------
    UNKNOWN = "unknown"


#: Expected stage per code. A DEFAULT and a test oracle - callers still supply
#: the factual stage. UNKNOWN is absent on purpose: it has no natural stage.
STAGE_FOR_CODE: Dict[DiscardCode, ScanStage] = {
    DiscardCode.DETAIL_CANCELLED_BEFORE_REQUEST: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_TRAFFIC_DENIED: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_RETRY_SLEEP_CANCELLED: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_NO_USABLE_RESPONSE: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_NO_FILENAME: ScanStage.DETAIL_PARSE,
    DiscardCode.DETAIL_PARSE_EXCEPTION: ScanStage.DETAIL_PARSE,
    DiscardCode.DETAIL_EMPTY: ScanStage.DETAIL_PARSE,
    DiscardCode.DETAIL_CANCELLED_BEFORE_START: ScanStage.DETAIL_FETCH,
    DiscardCode.DETAIL_CANCELLED_AFTER_START: ScanStage.DETAIL_FETCH,
    DiscardCode.MEDIA_ITEM_EXCEPTION: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.MISSING_REQUIRED_TITLE: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.MISSING_REQUIRED_URL: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.INVALID_METADATA: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.SOURCE_BLOCKED: ScanStage.DETAIL_FETCH,
}

#: Codes that mean "the scan was stopped", split by whether work had begun.
_CANCELLED_BEFORE_START = {DiscardCode.DETAIL_CANCELLED_BEFORE_START}
_CANCELLED_AFTER_START = {
    DiscardCode.DETAIL_CANCELLED_AFTER_START,
    DiscardCode.DETAIL_CANCELLED_BEFORE_REQUEST,
    DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR,
    DiscardCode.DETAIL_RETRY_SLEEP_CANCELLED,
}
#: Codes raised as exceptions rather than returned as absent data.
_RAISED = {
    DiscardCode.DETAIL_PARSE_EXCEPTION,
    DiscardCode.DETAIL_TRAFFIC_DENIED,
    DiscardCode.SOURCE_BLOCKED,
}

_MESSAGES: Dict[DiscardCode, str] = {
    DiscardCode.DETAIL_CANCELLED_BEFORE_REQUEST: "The scan stopped before this release's page was requested.",
    DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR: "The scan stopped while waiting for a request slot.",
    DiscardCode.DETAIL_TRAFFIC_DENIED: "The traffic coordinator declined the request.",
    DiscardCode.DETAIL_RETRY_SLEEP_CANCELLED: "The scan stopped between retry attempts.",
    DiscardCode.DETAIL_NO_USABLE_RESPONSE: "The page never returned a usable response after retries.",
    DiscardCode.DETAIL_NO_FILENAME: "The page loaded but no Filename field was found; the layout may have changed.",
    DiscardCode.DETAIL_PARSE_EXCEPTION: "Reading the page raised an error.",
    DiscardCode.DETAIL_EMPTY: "The scrape returned no data and did not report which step failed.",
    DiscardCode.DETAIL_CANCELLED_BEFORE_START: "The scan stopped before this release was looked at.",
    DiscardCode.DETAIL_CANCELLED_AFTER_START: "The scan stopped while this release was being processed.",
    DiscardCode.MEDIA_ITEM_EXCEPTION: "The page was read but the release could not be assembled.",
    DiscardCode.MISSING_REQUIRED_TITLE: "The release had no usable title.",
    DiscardCode.MISSING_REQUIRED_URL: "The release had no usable URL.",
    DiscardCode.INVALID_METADATA: "The release metadata failed validation.",
    DiscardCode.SOURCE_BLOCKED: "The source blocked the request.",
    DiscardCode.UNKNOWN: "The release was discarded for a reason the scan could not classify.",
}


def message_for(code: DiscardCode) -> str:
    return _MESSAGES.get(code, _MESSAGES[DiscardCode.UNKNOWN])


def default_stage_for(code: DiscardCode) -> ScanStage:
    """Fallback only. Callers supply the factual stage at the event site."""
    return STAGE_FOR_CODE.get(code, ScanStage.DETAIL_PARSE)


@dataclass(frozen=True)
class DiscardSample:
    """One bounded example. Carries no response body, ever."""

    canonical_url: str
    stage: str
    reason_code: str
    source: str = ""
    category: str = ""
    exception_type: Optional[str] = None
    #: Vocabulary version. Distinct from parser_version by design.
    taxonomy_version: int = TAXONOMY_VERSION
    #: The detail-parsing contract this failure was observed under. None until a
    #: real parser version exists - never silently copied from the taxonomy.
    parser_version: Optional[str] = None
    content_fingerprint: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "canonical_url": self.canonical_url,
            "stage": self.stage,
            "reason_code": self.reason_code,
            "source": self.source,
            "category": self.category,
            "exception_type": self.exception_type,
            "taxonomy_version": self.taxonomy_version,
            "parser_version": self.parser_version,
            "content_fingerprint": self.content_fingerprint,
        }


@dataclass
class ScanStageCounters:
    """Non-overlapping per-stage counters for one scan run.

    Thread-safe: detail counters increment from worker threads, and ``d[k] += 1``
    is three interruptible bytecodes that WILL lose updates at the configured
    thread count.
    """

    listing_pages_requested: int = 0
    listing_pages_succeeded: int = 0
    listing_urls_discovered: int = 0
    listing_urls_new: int = 0
    listing_urls_skipped_cached: int = 0
    listing_blocked_pages: int = 0
    listing_early_stopped: bool = False

    # scheduling vs execution vs cost - three different quantities
    detail_scheduled: int = 0
    detail_started: int = 0
    detail_http_requests: int = 0
    detail_cancelled_before_start: int = 0
    detail_cancelled_after_start: int = 0

    detail_returned_data: int = 0
    detail_returned_none: int = 0
    detail_raised_exception: int = 0

    media_item_created: int = 0
    media_item_construction_failed: int = 0

    reasons: Dict[str, int] = field(default_factory=dict)
    stages: Dict[str, int] = field(default_factory=dict)
    samples: List[DiscardSample] = field(default_factory=list)

    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # -- scheduling / execution ------------------------------------------

    def note_scheduled(self, count: int = 1) -> None:
        """Posts handed to the executor. Not yet attempted work."""
        if count <= 0:
            return
        with self._lock:
            self.detail_scheduled += count

    def note_started(self) -> None:
        """A worker actually entered this post."""
        with self._lock:
            self.detail_started += 1

    def note_http_request(self, count: int = 1) -> None:
        """One outbound detail request. A started post may make several."""
        if count <= 0:
            return
        with self._lock:
            self.detail_http_requests += count

    def note_detail_data(self) -> None:
        with self._lock:
            self.detail_returned_data += 1

    def note_item_created(self) -> None:
        with self._lock:
            self.media_item_created += 1

    # -- discards ---------------------------------------------------------

    def note_discard(
        self,
        code: DiscardCode,
        *,
        stage: Optional[ScanStage] = None,
        url: str = "",
        source: str = "",
        category: str = "",
        exception_type: Optional[str] = None,
        parser_version: Optional[str] = None,
        content_fingerprint: Optional[str] = None,
    ) -> None:
        """Record one discarded post at a FACTUAL stage.

        ``stage`` is supplied by the caller because only the event site knows
        where it happened; the code-to-stage map is a fallback for callers that
        genuinely have no better information.
        """
        actual = stage or default_stage_for(code)
        with self._lock:
            if code in _CANCELLED_BEFORE_START:
                self.detail_cancelled_before_start += 1
            elif code in _CANCELLED_AFTER_START:
                self.detail_cancelled_after_start += 1
            elif code in _RAISED:
                self.detail_raised_exception += 1
            elif actual is ScanStage.MEDIA_ITEM_CONSTRUCTION:
                self.media_item_construction_failed += 1
            else:
                self.detail_returned_none += 1

            key = code.value
            self.reasons[key] = self.reasons.get(key, 0) + 1
            self.stages[actual.value] = self.stages.get(actual.value, 0) + 1
            if self.reasons[key] <= MAX_SAMPLES_PER_REASON and url:
                self.samples.append(
                    DiscardSample(
                        canonical_url=url,
                        stage=actual.value,
                        reason_code=key,
                        source=source,
                        category=category,
                        exception_type=exception_type,
                        parser_version=parser_version,
                        content_fingerprint=content_fingerprint,
                    )
                )

    def note_cancelled_before_start(self, count: int) -> None:
        """Book futures the Stop press cancelled before any worker ran them.

        Must never touch detail_started or detail_http_requests: no request was
        made and no work began.
        """
        if count <= 0:
            return
        with self._lock:
            self.detail_cancelled_before_start += count
            key = DiscardCode.DETAIL_CANCELLED_BEFORE_START.value
            self.reasons[key] = self.reasons.get(key, 0) + count
            stage = ScanStage.DETAIL_FETCH.value
            self.stages[stage] = self.stages.get(stage, 0) + count

    # -- reporting --------------------------------------------------------

    @property
    def detail_parse_success_ratio(self) -> float:
        """Of the posts a worker actually started, how many yielded data."""
        if self.detail_started <= 0:
            return 1.0
        return self.detail_returned_data / float(self.detail_started)

    @property
    def media_item_construction_success_ratio(self) -> float:
        """Of the posts that yielded data, how many became releases."""
        if self.detail_returned_data <= 0:
            return 1.0
        return self.media_item_created / float(self.detail_returned_data)

    @property
    def end_to_end_item_yield(self) -> float:
        """Of the posts actually started, how many became releases."""
        if self.detail_started <= 0:
            return 1.0
        return self.media_item_created / float(self.detail_started)

    @property
    def requests_per_started_post(self) -> float:
        if self.detail_started <= 0:
            return 0.0
        return self.detail_http_requests / float(self.detail_started)

    def conservation_errors(self) -> List[str]:
        """Imbalances, as text. Empty means the books balance.

        Returns rather than raises: a bookkeeping bug must never break a scan.
        """
        errors: List[str] = []
        scheduled = self.detail_started + self.detail_cancelled_before_start
        if self.detail_scheduled != scheduled:
            errors.append(
                "detail_scheduled=%d != started+cancelled_before_start=%d"
                % (self.detail_scheduled, scheduled)
            )
        started = (
            self.detail_returned_data
            + self.detail_returned_none
            + self.detail_raised_exception
            + self.detail_cancelled_after_start
        )
        if self.detail_started != started:
            errors.append(
                "detail_started=%d != data+none+exception+cancelled_after_start=%d"
                % (self.detail_started, started)
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
            + self.detail_cancelled_before_start
            + self.detail_cancelled_after_start
            + self.media_item_construction_failed
        )
        tallied = sum(self.reasons.values())
        if tallied != discards:
            errors.append(
                "reason tally=%d != discard counters=%d" % (tallied, discards)
            )
        staged = sum(self.stages.values())
        if staged != tallied:
            errors.append("stage tally=%d != reason tally=%d" % (staged, tallied))
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
            "detail_scheduled": self.detail_scheduled,
            "detail_started": self.detail_started,
            "detail_http_requests": self.detail_http_requests,
            "detail_cancelled_before_start": self.detail_cancelled_before_start,
            "detail_cancelled_after_start": self.detail_cancelled_after_start,
            "detail_returned_data": self.detail_returned_data,
            "detail_returned_none": self.detail_returned_none,
            "detail_raised_exception": self.detail_raised_exception,
            "media_item_created": self.media_item_created,
            "media_item_construction_failed": self.media_item_construction_failed,
            "detail_parse_success_ratio": round(self.detail_parse_success_ratio, 4),
            "media_item_construction_success_ratio": round(
                self.media_item_construction_success_ratio, 4
            ),
            "end_to_end_item_yield": round(self.end_to_end_item_yield, 4),
            "requests_per_started_post": round(self.requests_per_started_post, 4),
            "reasons": dict(self.reasons),
            "stages": dict(self.stages),
            "samples": [s.to_dict() for s in self.samples],
            "conservation_errors": self.conservation_errors(),
        }

    def summary_line(self) -> str:
        """One aggregated line per phase. Never one per post."""
        head = "%d/%d started details produced releases (%d requests)" % (
            self.media_item_created,
            self.detail_started,
            self.detail_http_requests,
        )
        if not self.reasons:
            return head
        top = sorted(self.reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        return head + "; discarded: " + ", ".join(
            "%s=%d" % (code, n) for code, n in top
        )


class PostOutcome:
    """A single post's terminal-event ticket.

    Instrumentation spans two layers. The detail scraper knows which of its
    seven exits fired; the scanner knows about scheduling, cancellation and
    construction. Exactly one terminal event may be booked per post - otherwise
    a specific inner reason and a generic outer one both land and conservation
    breaks.

    Every method is no-throw: a recorder fault must not reach scan control flow.
    """

    __slots__ = ("_counters", "_url", "_source", "_category", "_booked", "_lock")

    def __init__(
        self,
        counters: Optional[ScanStageCounters],
        *,
        url: str = "",
        source: str = "",
        category: str = "",
    ) -> None:
        self._counters = counters
        self._url = url
        self._source = source
        self._category = category
        self._booked = False
        self._lock = threading.Lock()

    @property
    def booked(self) -> bool:
        """True once a terminal event has been recorded for this post."""
        return self._booked

    def _claim(self) -> bool:
        with self._lock:
            if self._booked:
                return False
            self._booked = True
            return True

    def note_started(self) -> None:
        try:
            if self._counters is not None:
                self._counters.note_started()
        except Exception:  # pragma: no cover - recorder must never escape
            pass

    def note_http_request(self, count: int = 1) -> None:
        try:
            if self._counters is not None:
                self._counters.note_http_request(count)
        except Exception:  # pragma: no cover
            pass

    def data_returned(self) -> None:
        """Detail data came back. Not terminal: construction may still fail."""
        try:
            if self._counters is not None:
                self._counters.note_detail_data()
        except Exception:  # pragma: no cover
            pass

    def item_created(self) -> None:
        try:
            if self._claim() and self._counters is not None:
                self._counters.note_item_created()
        except Exception:  # pragma: no cover
            pass

    def discard(
        self,
        code: DiscardCode,
        *,
        stage: Optional[ScanStage] = None,
        exception_type: Optional[str] = None,
        parser_version: Optional[str] = None,
        content_fingerprint: Optional[str] = None,
    ) -> bool:
        """Book the terminal outcome. Returns False if one was already booked."""
        try:
            if not self._claim():
                return False
            if self._counters is not None:
                self._counters.note_discard(
                    code,
                    stage=stage,
                    url=self._url,
                    source=self._source,
                    category=self._category,
                    exception_type=exception_type,
                    parser_version=parser_version,
                    content_fingerprint=content_fingerprint,
                )
            return True
        except Exception:  # pragma: no cover
            return False
