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

#: Second bound, so a pathological cycle cannot write an unbounded sample set
#: even across many distinct (reason, kind, stage) combinations.
MAX_SAMPLES_PER_CYCLE = 60


class ScanStage(str, Enum):
    LISTING = "listing"
    DETAIL_FETCH = "detail_fetch"
    DETAIL_PARSE = "detail_parse"
    #: Between a successful detail result and item construction. A Stop can
    #: strand a post here: the worker finished, but the main loop broke before
    #: consuming its result.
    DETAIL_TO_ITEM_HANDOFF = "detail_to_item_handoff"
    MEDIA_ITEM_CONSTRUCTION = "media_item_construction"
    #: No factual stage was supplied and none can be inferred. Kept visible
    #: rather than defaulted, so an omission cannot masquerade as a parse
    #: failure and send the investigation to the wrong layer.
    UNSPECIFIED = "unspecified"


class FutureTerminalState(str, Enum):
    """The only states a Future may be reconciled from.

    An exhaustive enum rather than combinable booleans, because a real Future
    cannot be both cancelled and carrying data. Booleans made that contradiction
    representable, and normalizing data on a cancelled future incremented
    detail_returned_data while booking CANCELLED_BEFORE_START.
    """

    CANCELLED_BEFORE_START = "cancelled_before_start"
    COMPLETED_WITH_DATA = "completed_with_data"
    COMPLETED_FALSEY = "completed_falsey"
    COMPLETED_EXCEPTION = "completed_exception"


class TerminalKind(str, Enum):
    """WHAT happened to a post's lifecycle - independent of stage and reason.

    Three dimensions answer three questions and none may be derived from
    another::

        stage         where was the post?
        terminal_kind what happened to its lifecycle?
        reason_code   which factual branch was observed?

    Deriving the conservation bucket from the reason code let an exceptional
    UNKNOWN be booked as "returned none": the equations balanced while the
    semantics were false.
    """

    CANCELLED_BEFORE_START = "cancelled_before_start"
    CANCELLED_AFTER_START = "cancelled_after_start"
    RETURNED_NONE = "returned_none"
    RAISED_EXCEPTION = "raised_exception"
    CONSTRUCTION_FAILED = "construction_failed"
    ABANDONED_ON_STOP = "abandoned_on_stop"
    TERMINAL_MISSING = "terminal_missing"
    #: No factual kind was supplied and none can be inferred. Never guess a
    #: content-failure bucket for an ambiguous event - that is how a green
    #: equation ends up describing something that did not happen.
    UNSPECIFIED = "unspecified"


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
    #: The detail succeeded; the scan stopped before its result was consumed.
    #: NOT a cancellation - the work completed - and NOT a failure.
    MEDIA_ITEM_ABANDONED_ON_STOP = "media_item_abandoned_on_stop"

    #: A post whose lifecycle could not be determined at reconciliation. This is
    #: a BOOKKEEPING DEFECT, never evidence that the post was cancelled.
    #: Labelling these as cancellation would balance the books by inventing a
    #: cause - the same error as a green test asserting the wrong invariant.
    TERMINAL_OUTCOME_MISSING = "terminal_outcome_missing"

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
    DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP: ScanStage.DETAIL_TO_ITEM_HANDOFF,
    DiscardCode.MISSING_REQUIRED_TITLE: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.MISSING_REQUIRED_URL: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.INVALID_METADATA: ScanStage.MEDIA_ITEM_CONSTRUCTION,
    DiscardCode.SOURCE_BLOCKED: ScanStage.DETAIL_FETCH,
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
    DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP: "The page was read successfully, but the scan stopped before the release was added.",
    DiscardCode.TERMINAL_OUTCOME_MISSING: "The scan could not determine what happened to this release; this is an instrumentation gap, not a known outcome.",
    DiscardCode.MISSING_REQUIRED_TITLE: "The release had no usable title.",
    DiscardCode.MISSING_REQUIRED_URL: "The release had no usable URL.",
    DiscardCode.INVALID_METADATA: "The release metadata failed validation.",
    DiscardCode.SOURCE_BLOCKED: "The source blocked the request.",
    DiscardCode.UNKNOWN: "The release was discarded for a reason the scan could not classify.",
}


def message_for(code: DiscardCode) -> str:
    return _MESSAGES.get(code, _MESSAGES[DiscardCode.UNKNOWN])


#: Expected lifecycle outcome per code. A DEFAULT and a test oracle only -
#: callers supply the factual kind, because one reason can end a post's life in
#: different ways (UNKNOWN may be an exception OR a falsy return).
DEFAULT_KIND_FOR_CODE: Dict[DiscardCode, TerminalKind] = {
    DiscardCode.DETAIL_CANCELLED_BEFORE_REQUEST: TerminalKind.CANCELLED_AFTER_START,
    DiscardCode.DETAIL_CANCELLED_IN_COORDINATOR: TerminalKind.CANCELLED_AFTER_START,
    DiscardCode.DETAIL_RETRY_SLEEP_CANCELLED: TerminalKind.CANCELLED_AFTER_START,
    DiscardCode.DETAIL_CANCELLED_AFTER_START: TerminalKind.CANCELLED_AFTER_START,
    DiscardCode.DETAIL_CANCELLED_BEFORE_START: TerminalKind.CANCELLED_BEFORE_START,
    DiscardCode.DETAIL_TRAFFIC_DENIED: TerminalKind.RAISED_EXCEPTION,
    DiscardCode.DETAIL_PARSE_EXCEPTION: TerminalKind.RAISED_EXCEPTION,
    DiscardCode.SOURCE_BLOCKED: TerminalKind.RAISED_EXCEPTION,
    DiscardCode.DETAIL_NO_USABLE_RESPONSE: TerminalKind.RETURNED_NONE,
    DiscardCode.DETAIL_NO_FILENAME: TerminalKind.RETURNED_NONE,
    DiscardCode.DETAIL_EMPTY: TerminalKind.RETURNED_NONE,
    DiscardCode.MEDIA_ITEM_EXCEPTION: TerminalKind.CONSTRUCTION_FAILED,
    DiscardCode.MISSING_REQUIRED_TITLE: TerminalKind.CONSTRUCTION_FAILED,
    DiscardCode.MISSING_REQUIRED_URL: TerminalKind.CONSTRUCTION_FAILED,
    DiscardCode.INVALID_METADATA: TerminalKind.CONSTRUCTION_FAILED,
    DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP: TerminalKind.ABANDONED_ON_STOP,
    DiscardCode.TERMINAL_OUTCOME_MISSING: TerminalKind.TERMINAL_MISSING,
    # UNKNOWN is deliberately ABSENT: it may be an exception, a falsy return or
    # a construction failure, and picking one silently asserts a lifecycle that
    # was never observed.
}

#: An operator pressing Stop is not a content failure.
_STOP_KINDS = {
    TerminalKind.CANCELLED_BEFORE_START,
    TerminalKind.CANCELLED_AFTER_START,
    TerminalKind.ABANDONED_ON_STOP,
}
#: A bookkeeping defect is not a content failure either.
_GAP_KINDS = {TerminalKind.TERMINAL_MISSING, TerminalKind.UNSPECIFIED}


def default_stage_for(code: DiscardCode) -> ScanStage:
    """Fallback only. Callers supply the factual stage at the event site.

    Returns UNSPECIFIED rather than a plausible guess: silently defaulting a
    stageless code to DETAIL_PARSE would file it against the wrong layer.
    """
    return STAGE_FOR_CODE.get(code, ScanStage.UNSPECIFIED)


def default_kind_for(code: DiscardCode) -> TerminalKind:
    """Fallback only. Callers supply the factual terminal kind.

    Returns UNSPECIFIED for anything unmapped, which files the event as an
    instrumentation gap rather than asserting a content failure that was never
    observed. A new reason code added without a kind must show up as a gap, not
    quietly inflate the returned-none bucket.
    """
    return DEFAULT_KIND_FOR_CODE.get(code, TerminalKind.UNSPECIFIED)


@dataclass(frozen=True)
class DiscardSample:
    """One bounded example. Carries no response body, ever."""

    canonical_url: str
    stage: str
    reason_code: str
    terminal_kind: str = TerminalKind.UNSPECIFIED.value
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
            "terminal_kind": self.terminal_kind,
            "source": self.source,
            "category": self.category,
            "exception_type": self.exception_type,
            "taxonomy_version": self.taxonomy_version,
            "parser_version": self.parser_version,
            "content_fingerprint": self.content_fingerprint,
        }


def _as_count(counters, value) -> int:
    """Coerce a caller-supplied count, recording a fault rather than raising.

    A recorder must never propagate a TypeError into scan control flow.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            with counters._lock:
                counters.recorder_faults += 1
        except Exception:  # pragma: no cover
            pass
        return 0


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
    #: Detail succeeded but the scan stopped before the result was consumed.
    #: A real lifecycle state, not a failure and not a cancellation.
    media_item_abandoned_on_stop: int = 0

    #: Instrumentation gaps, split by how far the post actually got. Kept as
    #: their own terms so the equations stay usable for detecting OTHER losses,
    #: while the defect stays named rather than absorbed into a
    #: plausible-looking bucket.
    #: reconcile() called without a terminal future state. A wiring defect.
    reconcile_misuse: int = 0
    #: A release was created after its ticket was already closed. A wiring
    #: defect, and invisible to the equations unless counted explicitly.
    media_item_created_after_terminal: int = 0
    #: Public recorder calls that raised internally and were swallowed.
    recorder_faults: int = 0

    scheduled_terminal_missing: int = 0
    detail_terminal_missing: int = 0
    media_item_terminal_missing: int = 0

    reasons: Dict[str, int] = field(default_factory=dict)
    stages: Dict[str, int] = field(default_factory=dict)
    kinds: Dict[str, int] = field(default_factory=dict)
    samples: List[DiscardSample] = field(default_factory=list)

    _sample_counts: Dict[tuple, int] = field(
        default_factory=dict, repr=False, compare=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    # -- scheduling / execution ------------------------------------------

    def note_scheduled(self, count: int = 1) -> None:
        """Posts handed to the executor. Not yet attempted work."""
        count = _as_count(self, count)
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
        count = _as_count(self, count)
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

    def note_created_after_terminal(self) -> None:
        """A release shipped, but the ticket had already been closed.

        Named and conserved rather than dropped: ABANDONED_ON_STOP occupies the
        same slot in the returned-data equation, so a silently discarded
        creation was cancelled out one-for-one and the books stayed clean while
        a genuinely shipped release went uncounted.
        """
        with self._lock:
            self.media_item_created_after_terminal += 1

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
        lifecycle_started: Optional[bool] = None,
        terminal_kind: Optional[TerminalKind] = None,
    ) -> None:
        """Record one discarded post at a FACTUAL stage and terminal kind.

        ``stage`` is supplied by the caller because only the event site knows
        where it happened; the code-to-stage map is a fallback for callers that
        genuinely have no better information. ``lifecycle_started`` lets an
        instrumentation gap be filed against the equation it actually breaks.
        """
        # Coerce and validate BEFORE taking the lock. note_discard performs
        # five mutations under one lock; a raise partway through used to leave
        # the object permanently inconsistent with no way to tell which post
        # caused the drift.
        try:
            code = DiscardCode(code)
        except (ValueError, TypeError):
            code = DiscardCode.UNKNOWN
        try:
            actual = ScanStage(stage) if stage is not None else default_stage_for(code)
        except (ValueError, TypeError):
            actual = ScanStage.UNSPECIFIED
        try:
            kind = (
                TerminalKind(terminal_kind)
                if terminal_kind is not None
                else default_kind_for(code)
            )
        except (ValueError, TypeError):
            kind = TerminalKind.UNSPECIFIED
        post_data = actual in (
            ScanStage.DETAIL_TO_ITEM_HANDOFF,
            ScanStage.MEDIA_ITEM_CONSTRUCTION,
        )
        with self._lock:
            if kind is TerminalKind.ABANDONED_ON_STOP:
                self.media_item_abandoned_on_stop += 1
            elif kind in _GAP_KINDS:
                # File the gap against the equation it actually breaks, so a
                # post that never ran does not masquerade as started work.
                if post_data:
                    self.media_item_terminal_missing += 1
                elif lifecycle_started is False:
                    self.scheduled_terminal_missing += 1
                else:
                    self.detail_terminal_missing += 1
            elif kind is TerminalKind.CANCELLED_BEFORE_START:
                self.detail_cancelled_before_start += 1
            elif kind is TerminalKind.CANCELLED_AFTER_START:
                self.detail_cancelled_after_start += 1
            elif kind is TerminalKind.RAISED_EXCEPTION:
                self.detail_raised_exception += 1
            elif kind is TerminalKind.CONSTRUCTION_FAILED:
                self.media_item_construction_failed += 1
            else:
                self.detail_returned_none += 1

            key = code.value
            self.reasons[key] = self.reasons.get(key, 0) + 1
            self.stages[actual.value] = self.stages.get(actual.value, 0) + 1
            self.kinds[kind.value] = self.kinds.get(kind.value, 0) + 1
            # Cap per (reason, kind, stage): capping by reason alone meant the
            # first five returned-none UNKNOWNs starved a later exceptional
            # UNKNOWN of any sample at all.
            bucket = (key, kind.value, actual.value)
            self._sample_counts[bucket] = self._sample_counts.get(bucket, 0) + 1
            if (
                url
                and self._sample_counts[bucket] <= MAX_SAMPLES_PER_REASON
                and len(self.samples) < MAX_SAMPLES_PER_CYCLE
            ):
                self.samples.append(
                    DiscardSample(
                        canonical_url=url,
                        stage=actual.value,
                        reason_code=key,
                        terminal_kind=kind.value,
                        source=source,
                        category=category,
                        exception_type=exception_type,
                        parser_version=parser_version,
                        content_fingerprint=content_fingerprint,
                    )
                )

    def note_reconcile_misuse(self) -> None:
        """A reconcile() call that supplied no terminal future state.

        Counted, never silently ignored: it means the wiring reconciled a post
        whose future had not finished, which would race the worker's own
        recording.
        """
        with self._lock:
            self.reconcile_misuse += 1

    def note_cancelled_before_start(self, count: int) -> None:
        """Book futures the Stop press cancelled before any worker ran them.

        Must never touch detail_started or detail_http_requests: no request was
        made and no work began.
        """
        count = _as_count(self, count)
        if count <= 0:
            return
        with self._lock:
            self.detail_cancelled_before_start += count
            key = DiscardCode.DETAIL_CANCELLED_BEFORE_START.value
            self.reasons[key] = self.reasons.get(key, 0) + count
            stage = ScanStage.DETAIL_FETCH.value
            self.stages[stage] = self.stages.get(stage, 0) + count
            kind = TerminalKind.CANCELLED_BEFORE_START.value
            self.kinds[kind] = self.kinds.get(kind, 0) + count

    # -- reporting --------------------------------------------------------
    #
    # EVERY reader works from ONE atomic snapshot. Reading the counters and the
    # three dicts unlocked produced two failures, both measured: a RuntimeError
    # ("dictionary changed size during iteration") escaping a public method -
    # the one thing this module promises cannot happen - and phantom
    # conservation errors on 99.9% of concurrent reads, because the equations
    # compared a newer left-hand side against an older right-hand side. The
    # loudest "the instrumentation is lying" signal fired because the READER
    # was lying.
    #
    # The lock is not reentrant, so the arithmetic lives in module-level pure
    # functions over a snapshot and no public reader takes the lock twice.

    def snapshot_counts(self) -> "CountersSnapshot":
        """One consistent view of every counter, taken under the lock."""
        with self._lock:
            return CountersSnapshot(
                listing_pages_requested=self.listing_pages_requested,
                listing_pages_succeeded=self.listing_pages_succeeded,
                listing_urls_discovered=self.listing_urls_discovered,
                listing_urls_new=self.listing_urls_new,
                listing_urls_skipped_cached=self.listing_urls_skipped_cached,
                listing_blocked_pages=self.listing_blocked_pages,
                listing_early_stopped=bool(self.listing_early_stopped),
                detail_scheduled=self.detail_scheduled,
                detail_started=self.detail_started,
                detail_http_requests=self.detail_http_requests,
                detail_cancelled_before_start=self.detail_cancelled_before_start,
                detail_cancelled_after_start=self.detail_cancelled_after_start,
                detail_returned_data=self.detail_returned_data,
                detail_returned_none=self.detail_returned_none,
                detail_raised_exception=self.detail_raised_exception,
                media_item_created=self.media_item_created,
                media_item_construction_failed=self.media_item_construction_failed,
                media_item_abandoned_on_stop=self.media_item_abandoned_on_stop,
                media_item_created_after_terminal=self.media_item_created_after_terminal,
                reconcile_misuse=self.reconcile_misuse,
                recorder_faults=self.recorder_faults,
                scheduled_terminal_missing=self.scheduled_terminal_missing,
                detail_terminal_missing=self.detail_terminal_missing,
                media_item_terminal_missing=self.media_item_terminal_missing,
                reasons=dict(self.reasons),
                stages=dict(self.stages),
                kinds=dict(self.kinds),
                sample_count=len(self.samples),
            )

    def outcome_groups(self) -> Dict[str, int]:
        return outcome_groups_of(self.snapshot_counts())

    def conservation_errors(self) -> List[str]:
        return conservation_errors_of(self.snapshot_counts())

    @property
    def detail_parse_success_ratio(self) -> float:
        return detail_parse_success_ratio_of(self.snapshot_counts())

    @property
    def media_item_construction_attempted(self) -> int:
        snap = self.snapshot_counts()
        return snap.media_item_created + snap.media_item_construction_failed

    @property
    def media_item_construction_success_ratio(self) -> float:
        return construction_success_ratio_of(self.snapshot_counts())

    @property
    def end_to_end_item_yield(self) -> float:
        return end_to_end_item_yield_of(self.snapshot_counts())

    @property
    def requests_per_started_post(self) -> Optional[float]:
        return requests_per_started_post_of(self.snapshot_counts())

    @property
    def eligible_for_health_scoring(self) -> bool:
        return eligible_for_health_scoring_of(self.snapshot_counts())

    @property
    def eligible_for_construction_scoring(self) -> bool:
        return eligible_for_construction_scoring_of(self.snapshot_counts())

    def to_dict(self) -> dict:
        """Serialize from ONE snapshot.

        Previously this called four readers that each took their own view, so a
        single published row could contradict itself.
        """
        snap = self.snapshot_counts()
        with self._lock:
            samples = [sample.to_dict() for sample in self.samples]
        return dict_of(snap, samples)

    def summary_line(self) -> str:
        return summary_line_of(self.snapshot_counts())


@dataclass(frozen=True)
class CountersSnapshot:
    """An immutable, self-consistent view of one scan's counters."""

    listing_pages_requested: int
    listing_pages_succeeded: int
    listing_urls_discovered: int
    listing_urls_new: int
    listing_urls_skipped_cached: int
    listing_blocked_pages: int
    listing_early_stopped: bool
    detail_scheduled: int
    detail_started: int
    detail_http_requests: int
    detail_cancelled_before_start: int
    detail_cancelled_after_start: int
    detail_returned_data: int
    detail_returned_none: int
    detail_raised_exception: int
    media_item_created: int
    media_item_construction_failed: int
    media_item_abandoned_on_stop: int
    media_item_created_after_terminal: int
    reconcile_misuse: int
    recorder_faults: int
    scheduled_terminal_missing: int
    detail_terminal_missing: int
    media_item_terminal_missing: int
    reasons: Dict[str, int]
    stages: Dict[str, int]
    kinds: Dict[str, int]
    sample_count: int


def outcome_groups_of(snap: CountersSnapshot) -> Dict[str, int]:
    """Split discards into populations that must not be conflated.

    A parser-regression threshold that counts an operator pressing Stop, or
    counts our own bookkeeping gaps, measures the wrong thing.
    """
    groups = {"failures": 0, "operator_stop_outcomes": 0, "instrumentation_gaps": 0}
    for name, count in snap.kinds.items():
        try:
            kind = TerminalKind(name)
        except (ValueError, TypeError):
            # An unparseable key is itself a bookkeeping defect, not a failure.
            groups["instrumentation_gaps"] += count
            continue
        if kind in _STOP_KINDS:
            groups["operator_stop_outcomes"] += count
        elif kind in _GAP_KINDS:
            groups["instrumentation_gaps"] += count
        else:
            groups["failures"] += count
    return groups


def detail_parse_success_ratio_of(snap: CountersSnapshot) -> float:
    """Of the posts that actually REACHED parsing, how many yielded data.

    Cancellations and instrumentation gaps are excluded from the denominator:
    including them made a total source outage and a total parser regression
    produce an identical number.
    """
    attempted = (
        snap.detail_returned_data
        + snap.detail_returned_none
        + snap.detail_raised_exception
    )
    if attempted <= 0:
        return 1.0
    return snap.detail_returned_data / float(attempted)


def construction_success_ratio_of(snap: CountersSnapshot) -> float:
    attempted = snap.media_item_created + snap.media_item_construction_failed
    if attempted <= 0:
        return 1.0
    return snap.media_item_created / float(attempted)


def end_to_end_item_yield_of(snap: CountersSnapshot) -> float:
    if snap.detail_started <= 0:
        return 1.0
    return snap.media_item_created / float(snap.detail_started)


def requests_per_started_post_of(snap: CountersSnapshot) -> Optional[float]:
    """None when there is nothing to divide - never 0.0, which is a real value
    inside this metric's normal range and would be indistinguishable from a
    scan that genuinely made no requests."""
    if snap.detail_started <= 0:
        return None
    return snap.detail_http_requests / float(snap.detail_started)


def eligible_for_health_scoring_of(snap: CountersSnapshot) -> bool:
    """Whether this cycle may inform parser-health judgements.

    Requires observations and books that balance. No observations is not
    evidence of perfect health, and a cycle whose own bookkeeping is broken is
    not evidence of anything. Stop outcomes are excluded from the RATIO
    DENOMINATORS rather than vetoing the cycle - one routine cancellation used
    to silence the very regression these counters exist to catch.
    """
    groups = outcome_groups_of(snap)
    return (
        snap.detail_started > 0
        and groups["instrumentation_gaps"] == 0
        and snap.reconcile_misuse == 0
        and snap.media_item_created_after_terminal == 0
        and snap.recorder_faults == 0
        and not conservation_errors_of(snap)
    )


def eligible_for_construction_scoring_of(snap: CountersSnapshot) -> bool:
    attempted = snap.media_item_created + snap.media_item_construction_failed
    return eligible_for_health_scoring_of(snap) and attempted > 0


def conservation_errors_of(snap: CountersSnapshot) -> List[str]:
    """Imbalances, as text. Empty means the books balance.

    Returns rather than raises: a bookkeeping bug must never break a scan.
    """
    errors: List[str] = []
    scheduled = (
        snap.detail_started
        + snap.detail_cancelled_before_start
        + snap.scheduled_terminal_missing
    )
    if snap.detail_scheduled != scheduled:
        errors.append(
            "detail_scheduled=%d != started+cancelled_before_start+terminal_missing=%d"
            % (snap.detail_scheduled, scheduled)
        )
    started = (
        snap.detail_returned_data
        + snap.detail_returned_none
        + snap.detail_raised_exception
        + snap.detail_cancelled_after_start
        + snap.detail_terminal_missing
    )
    if snap.detail_started != started:
        errors.append(
            "detail_started=%d != data+none+exception+cancelled_after_start"
            "+terminal_missing=%d" % (snap.detail_started, started)
        )
    constructed = (
        snap.media_item_created
        + snap.media_item_construction_failed
        + snap.media_item_abandoned_on_stop
        + snap.media_item_terminal_missing
    )
    if snap.detail_returned_data != constructed:
        errors.append(
            "detail_returned_data=%d != created+construction_failed"
            "+abandoned_on_stop+terminal_missing=%d"
            % (snap.detail_returned_data, constructed)
        )
    discards = (
        snap.detail_returned_none
        + snap.detail_raised_exception
        + snap.detail_cancelled_before_start
        + snap.detail_cancelled_after_start
        + snap.scheduled_terminal_missing
        + snap.detail_terminal_missing
        + snap.media_item_construction_failed
        + snap.media_item_abandoned_on_stop
        + snap.media_item_terminal_missing
    )
    tallied = sum(snap.reasons.values())
    if tallied != discards:
        errors.append("reason tally=%d != discard counters=%d" % (tallied, discards))
    staged = sum(snap.stages.values())
    if staged != tallied:
        errors.append("stage tally=%d != reason tally=%d" % (staged, tallied))
    kinded = sum(snap.kinds.values())
    if kinded != tallied:
        errors.append("kind tally=%d != reason tally=%d" % (kinded, tallied))
    # Every recorded key must parse back to its enum. The old group-vs-kind
    # check could not fail by construction, and silently absorbed an unparseable
    # kind into instrumentation_gaps with clean books.
    for label, mapping, enum_cls in (
        ("reason", snap.reasons, DiscardCode),
        ("stage", snap.stages, ScanStage),
        ("kind", snap.kinds, TerminalKind),
    ):
        for key in mapping:
            try:
                enum_cls(key)
            except (ValueError, TypeError):
                errors.append("unrecognised %s key %r" % (label, key))
    if snap.media_item_created_after_terminal:
        errors.append(
            "media_item_created_after_terminal=%d releases shipped after their "
            "ticket closed" % snap.media_item_created_after_terminal
        )
    return errors


def dict_of(snap: CountersSnapshot, samples: List[dict]) -> dict:
    groups = outcome_groups_of(snap)
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "listing_pages_requested": snap.listing_pages_requested,
        "listing_pages_succeeded": snap.listing_pages_succeeded,
        "listing_urls_discovered": snap.listing_urls_discovered,
        "listing_urls_new": snap.listing_urls_new,
        "listing_urls_skipped_cached": snap.listing_urls_skipped_cached,
        "listing_blocked_pages": snap.listing_blocked_pages,
        "listing_early_stopped": snap.listing_early_stopped,
        "detail_scheduled": snap.detail_scheduled,
        "detail_started": snap.detail_started,
        "detail_http_requests": snap.detail_http_requests,
        "detail_cancelled_before_start": snap.detail_cancelled_before_start,
        "detail_cancelled_after_start": snap.detail_cancelled_after_start,
        "detail_returned_data": snap.detail_returned_data,
        "detail_returned_none": snap.detail_returned_none,
        "detail_raised_exception": snap.detail_raised_exception,
        "media_item_created": snap.media_item_created,
        "media_item_construction_failed": snap.media_item_construction_failed,
        "media_item_abandoned_on_stop": snap.media_item_abandoned_on_stop,
        "media_item_created_after_terminal": snap.media_item_created_after_terminal,
        "media_item_construction_attempted": (
            snap.media_item_created + snap.media_item_construction_failed
        ),
        "reconcile_misuse": snap.reconcile_misuse,
        "recorder_faults": snap.recorder_faults,
        "scheduled_terminal_missing": snap.scheduled_terminal_missing,
        "detail_terminal_missing": snap.detail_terminal_missing,
        "media_item_terminal_missing": snap.media_item_terminal_missing,
        "detail_parse_success_ratio": round(detail_parse_success_ratio_of(snap), 4),
        "media_item_construction_success_ratio": round(
            construction_success_ratio_of(snap), 4
        ),
        "end_to_end_item_yield": round(end_to_end_item_yield_of(snap), 4),
        "requests_per_started_post": requests_per_started_post_of(snap),
        "reasons": dict(snap.reasons),
        "stages": dict(snap.stages),
        "kinds": dict(snap.kinds),
        "outcome_groups": groups,
        "eligible_for_health_scoring": eligible_for_health_scoring_of(snap),
        "eligible_for_construction_scoring": eligible_for_construction_scoring_of(snap),
        "samples": samples,
        "conservation_errors": conservation_errors_of(snap),
    }


def summary_line_of(snap: CountersSnapshot) -> str:
    """One aggregated line per phase. Never one per post."""
    head = "%d/%d started details produced releases (%d requests)" % (
        snap.media_item_created,
        snap.detail_started,
        snap.detail_http_requests,
    )
    errors = conservation_errors_of(snap)
    if not snap.reasons:
        return head + ("; metrics_errors=%d" % len(errors) if errors else "")
    groups = outcome_groups_of(snap)
    top = sorted(snap.reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    detail = ", ".join("%s=%d" % (code, n) for code, n in top)
    parts = [head]
    if groups["failures"]:
        parts.append("failures=%d" % groups["failures"])
    if groups["operator_stop_outcomes"]:
        parts.append("stopped=%d" % groups["operator_stop_outcomes"])
    gaps = groups["instrumentation_gaps"] + snap.reconcile_misuse
    if gaps:
        parts.append("instrumentation gaps=%d" % gaps)
    if errors:
        parts.append("metrics_errors=%d" % len(errors))
    return "; ".join(parts) + " [" + detail + "]"


class PostOutcome:
    """A single post's terminal-event ticket.

    Instrumentation spans two layers. The detail scraper knows which of its
    seven exits fired; the scanner knows about scheduling, cancellation and
    construction. Exactly one terminal event may be booked per post - otherwise
    a specific inner reason and a generic outer one both land and conservation
    breaks.

    Every method is no-throw: a recorder fault must not reach scan control flow.
    """

    __slots__ = (
        "_counters", "_url", "_source", "_category",
        "_started", "_data_returned", "_booked", "_terminal_code",
        "_exception_type", "_lock",
    )

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
        self._started = False
        self._data_returned = False
        self._booked = False
        self._terminal_code: Optional[str] = None
        self._exception_type: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return self._url

    @property
    def booked(self) -> bool:
        """Whether a terminal event exists. Convenience for the ordinary path.

        Reconciliation must use :meth:`snapshot` instead - it needs several
        lifecycle fields read together, and assembling those from separate
        unlocked reads is not a synchronization contract.
        """
        with self._lock:
            return self._booked

    def snapshot(self) -> "OutcomeSnapshot":
        """An atomic lifecycle view, taken under the ticket lock.

        Post-drain reconciliation reads this while workers may still be
        finishing, so it must not be assembled from separate unlocked reads.
        """
        with self._lock:
            return OutcomeSnapshot(
                url=self._url,
                started=self._started,
                data_returned=self._data_returned,
                terminal_booked=self._booked,
                terminal_code=self._terminal_code,
            )

    def _claim(self, code: Optional[DiscardCode]) -> bool:
        # Resolve the label BEFORE claiming: computing it inside the critical
        # section meant a bad code poisoned the ticket with terminal_code=None
        # and disarmed both the retry and the outer fallback.
        try:
            label = DiscardCode(code).value if code is not None else "item_created"
        except (ValueError, TypeError):
            label = DiscardCode.UNKNOWN.value
        with self._lock:
            if self._booked:
                return False
            self._booked = True
            self._terminal_code = label
            return True

    def _release_claim(self) -> None:
        """Undo a claim whose recording failed, so the post stays diagnosable."""
        with self._lock:
            self._booked = False
            self._terminal_code = None

    def note_started(self) -> None:
        """Idempotent: a retry inside the worker must not re-count the post."""
        try:
            with self._lock:
                if self._started:
                    return
                self._started = True
            if self._counters is not None:
                self._counters.note_started()
        except Exception:  # pragma: no cover - recorder must never escape
            pass

    def note_http_request(self, count: int = 1) -> None:
        """Not idempotent by design: each retry is a real extra request."""
        try:
            if self._counters is not None:
                self._counters.note_http_request(count)
        except Exception:  # pragma: no cover
            pass

    def data_returned(self) -> None:
        """Detail data came back. Idempotent, and NOT terminal - construction,
        abandonment on Stop, or a gap may still follow."""
        try:
            with self._lock:
                if self._data_returned:
                    return
                self._data_returned = True
            if self._counters is not None:
                self._counters.note_detail_data()
        except Exception:  # pragma: no cover
            pass

    def item_created(self) -> bool:
        """Book a shipped release. False if the ticket was already closed.

        Symmetric with :meth:`discard` on purpose. Previously this was the only
        increment gated on winning the claim, and a lost claim dropped the event
        with no return value and no counter - so a scan that shipped 40 releases
        could report zero with perfectly clean books.
        """
        try:
            if self._claim(None):
                if self._counters is not None:
                    self._counters.note_item_created()
                return True
            if self._counters is not None:
                self._counters.note_created_after_terminal()
            return False
        except Exception:  # pragma: no cover
            return False

    def discard(
        self,
        code: DiscardCode,
        *,
        stage: Optional[ScanStage] = None,
        exception_type: Optional[str] = None,
        parser_version: Optional[str] = None,
        content_fingerprint: Optional[str] = None,
        lifecycle_started: Optional[bool] = None,
        terminal_kind: Optional[TerminalKind] = None,
    ) -> bool:
        """Book the terminal outcome. Returns False if one was already booked."""
        try:
            if not self._claim(code):
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
                    lifecycle_started=lifecycle_started,
                    terminal_kind=terminal_kind,
                )
            return True
        except Exception:  # pragma: no cover - recorder must never escape
            # Give the claim back. A recorder fault must not permanently
            # silence a post - the exact outcome this module exists to prevent.
            self._release_claim()
            return False

    # -- post-drain reconciliation ---------------------------------------

    def reconcile(self, state: Optional[FutureTerminalState] = None) -> Optional[DiscardCode]:
        """Close a ticket against a TERMINAL future state.

        ``state`` is REQUIRED and exhaustive. Passing nothing means terminality
        was never established, so the ticket is NOT claimed and no terminal
        event is booked - a premature call must not steal the claim from a
        worker that is still running and about to record its exact branch. The
        misuse is counted separately via :attr:`reconcile_misuse`.

        Facts the future proves are normalized first: a completed non-cancelled
        future proves its worker ran, and a data-carrying result proves data
        came back, even if the worker recorded neither. Both transitions are
        idempotent, so backfilling is safe. A cancelled future is never
        backfilled - it never ran.

        Every branch names a REAL lifecycle state. Nothing is labelled
        cancellation to balance the books.
        """
        if state is None:
            # Not terminal (or the caller did not say). Leave the ticket open.
            self._note_misuse()
            return None

        if state is not FutureTerminalState.CANCELLED_BEFORE_START:
            self.note_started()                              # idempotent
        if state is FutureTerminalState.COMPLETED_WITH_DATA:
            self.data_returned()                             # idempotent

        snap = self.snapshot()
        if snap.terminal_booked:
            return None

        if state is FutureTerminalState.CANCELLED_BEFORE_START:
            code = DiscardCode.DETAIL_CANCELLED_BEFORE_START
            stage = ScanStage.DETAIL_FETCH
            kind = TerminalKind.CANCELLED_BEFORE_START
        elif state is FutureTerminalState.COMPLETED_EXCEPTION and snap.data_returned:
            # The scraper produced its dict and the worker raised downstream.
            # Nothing was lost at parse; the failure is on the construction side.
            code = DiscardCode.MEDIA_ITEM_EXCEPTION
            stage = ScanStage.MEDIA_ITEM_CONSTRUCTION
            kind = TerminalKind.CONSTRUCTION_FAILED
        elif state is FutureTerminalState.COMPLETED_EXCEPTION:
            # The reason is unknown; the LIFECYCLE is not - it raised.
            code = DiscardCode.UNKNOWN
            stage = ScanStage.UNSPECIFIED
            kind = TerminalKind.RAISED_EXCEPTION
        elif state is FutureTerminalState.COMPLETED_WITH_DATA:
            # The detail SUCCEEDED; the scan stopped before its result was used.
            code = DiscardCode.MEDIA_ITEM_ABANDONED_ON_STOP
            stage = ScanStage.DETAIL_TO_ITEM_HANDOFF
            kind = TerminalKind.ABANDONED_ON_STOP
        elif snap.data_returned:
            # The future says falsy, the ticket says data. A real contradiction:
            # name it as an instrumentation gap rather than inventing an
            # operator Stop that never happened.
            code = DiscardCode.TERMINAL_OUTCOME_MISSING
            stage = ScanStage.DETAIL_TO_ITEM_HANDOFF
            kind = TerminalKind.TERMINAL_MISSING
        else:
            # Ran, returned falsy, booked nothing: an uninstrumented scraper.
            code = DiscardCode.DETAIL_EMPTY
            stage = ScanStage.DETAIL_PARSE
            kind = TerminalKind.RETURNED_NONE

        booked = self.discard(
            code,
            stage=stage,
            exception_type=self._exception_type,
            lifecycle_started=snap.started,
            terminal_kind=kind,
        )
        return code if booked else None

    def note_exception_type(self, exception_type: Optional[str]) -> None:
        """Attach a bounded exception class name for the diagnostic sample."""
        self._exception_type = exception_type

    def _note_misuse(self) -> None:
        try:
            if self._counters is not None:
                self._counters.note_reconcile_misuse()
        except Exception:  # pragma: no cover
            pass


@dataclass(frozen=True)
class OutcomeSnapshot:
    """Immutable lifecycle view of a PostOutcome, taken under its lock."""

    url: str
    started: bool
    data_returned: bool
    terminal_booked: bool
    terminal_code: Optional[str]
