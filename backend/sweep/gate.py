"""The lag-aware gate — three state models kept deliberately separate.

Track A's whole finding was that ONE state model conflated two different
questions and therefore answered both wrong. "100 shadow misses" turned out to
be 99 items acquired normally with a median observation lag of ~1 h: the FEED
was a little slow, and the PRODUCT was never uncovered. A single "missed" label
could not express that, so it reported a coverage crisis that did not exist.

Hence three models that are computed independently and never collapsed:

    RSS acquisition   — how fast the FEED surfaced it       (a health metric)
    identity coverage — whether WE ended up with it          (the real outcome)
    source interval   — whether the SOURCE is still trusted  (health.py)

The load-bearing consequence, design rev 2.1 §8: *a RED RSS item recovered by a
complete sweep is an RSS-health metric, not an uncovered release.* It degrades
the feed's score and leaves coverage intact. Anything that reads the two as one
number reproduces the original error.

LAG AWARENESS means an item younger than the acquisition band is `pending` — not
missing. An item that has existed for twenty minutes has not been missed by a
6-hour band; declaring it missed is measuring the clock, not the pipeline.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from backend.sweep.health import IntervalState, SourceHealth

GREEN_HOURS = 6.0
YELLOW_HOURS = 24.0

#: §10 request-cost floor. Below this, promotion fails regardless of coverage.
MIN_REQUEST_REDUCTION = 0.50
TARGET_REQUEST_REDUCTION = 0.70


class RssAcquisition(str, Enum):
    """Measures the FEED. Never a coverage verdict on its own."""
    PENDING = "pending"        # younger than the band — not yet judgeable
    GREEN = "green"            # normal feed within 6 h
    YELLOW = "yellow"          # normal feed 6–24 h
    RED = "red"                # >24 h, or never surfaced by the normal feed
    AMBIGUOUS = "ambiguous"    # attribution cannot be established


class IdentityCoverage(str, Enum):
    """Measures the PRODUCT — what we actually ended up holding."""
    COVERED_BY_RSS = "covered_by_rss"
    COVERED_BY_SWEEP = "covered_by_sweep"
    RSS_RED_COVERED_BY_SWEEP = "rss_red_covered_by_sweep"
    AMBIGUOUS_IDENTITY = "ambiguous_identity"
    PROCESSING_FAILED = "processing_failed"


#: Coverage states that mean we hold the release. The RED-recovered case counts
#: as covered — that is the §8 rule made executable.
COVERED = {
    IdentityCoverage.COVERED_BY_RSS,
    IdentityCoverage.COVERED_BY_SWEEP,
    IdentityCoverage.RSS_RED_COVERED_BY_SWEEP,
}


@dataclass(frozen=True)
class ItemVerdict:
    canonical_url: str
    rss_state: RssAcquisition
    #: None while the item is `pending`. The five coverage states classify items
    #: old enough to judge; an item inside the acquisition band has no coverage
    #: answer yet. Calling it `covered_by_rss` would assert an acquisition that
    #: has not happened, and calling it a gap would assert a miss that has not
    #: happened either — so the honest value is "no answer yet".
    coverage_state: Optional[IdentityCoverage]
    normal_feed_latency_hours: Optional[float]
    detail: str

    @property
    def judgeable(self) -> bool:
        return self.coverage_state is not None

    @property
    def is_covered(self) -> bool:
        return self.coverage_state in COVERED

    @property
    def is_gap(self) -> bool:
        """A real coverage gap: judgeable, and not held by any route."""
        return self.judgeable and self.coverage_state not in COVERED

    @property
    def degrades_feed_health(self) -> bool:
        """True when the FEED performed badly, regardless of coverage."""
        return self.rss_state in (RssAcquisition.YELLOW, RssAcquisition.RED,
                                  RssAcquisition.AMBIGUOUS)


def classify_item(
    item,
    *,
    now: dt.datetime,
    green_hours: float = GREEN_HOURS,
    yellow_hours: float = YELLOW_HOURS,
) -> ItemVerdict:
    """Classify one release against both models.

    `item` carries:
      published_at       — when the source displayed it (observation-derived)
      first_normal_at    — first appearance in a NORMAL feed poll, or None
      first_sweep_at     — first observation by a listing sweep, or None
      sweep_complete     — whether the sweep that saw it reached completion
      identity_ambiguous — canonicalisation could not resolve it uniquely
      processing_failed  — acquired, but the pipeline failed to handle it
    """
    url = item.get("canonical_url", "")
    published = _parse(item.get("published_at"))
    first_normal = _parse(item.get("first_normal_at"))
    first_sweep = _parse(item.get("first_sweep_at"))

    # Ambiguity is decided before anything else: an item we cannot identify
    # cannot be scored on either axis, and guessing would be the failure mode
    # both models exist to prevent.
    if item.get("identity_ambiguous"):
        return ItemVerdict(url, RssAcquisition.AMBIGUOUS,
                           IdentityCoverage.AMBIGUOUS_IDENTITY, None,
                           "identity could not be resolved uniquely")

    # ── the FEED axis ────────────────────────────────────────────────────
    latency = None
    if first_normal and published:
        latency = (first_normal - published).total_seconds() / 3600.0

    age_hours = (now - published).total_seconds() / 3600.0 if published else None

    if published is None:
        rss_state = RssAcquisition.AMBIGUOUS
        rss_detail = "no publication time to measure latency against"
    elif latency is not None:
        # Latency is measured from first_normal_at, never from a catch-up or
        # sweep observation — round 6 required this and it is the reason the
        # original "0 of 100 acquired" figure was wrong.
        if latency <= green_hours:
            rss_state = RssAcquisition.GREEN
        elif latency <= yellow_hours:
            rss_state = RssAcquisition.YELLOW
        else:
            rss_state = RssAcquisition.RED
        rss_detail = f"normal feed surfaced it after {latency:.2f} h"
    elif age_hours is not None and age_hours < green_hours:
        # LAG AWARENESS. Not yet acquired, but not yet late either.
        rss_state = RssAcquisition.PENDING
        rss_detail = f"only {age_hours:.2f} h old — inside the {green_hours:.0f} h band"
    elif age_hours is not None and age_hours <= yellow_hours:
        rss_state = RssAcquisition.YELLOW
        rss_detail = f"{age_hours:.2f} h old and not yet in a normal feed"
    else:
        rss_state = RssAcquisition.RED
        rss_detail = f"{age_hours:.2f} h old and never surfaced by the normal feed"

    # ── the PRODUCT axis ─────────────────────────────────────────────────
    acquired_by_sweep = bool(first_sweep and item.get("sweep_complete"))

    if item.get("processing_failed"):
        coverage = IdentityCoverage.PROCESSING_FAILED
        cov_detail = "acquired but the pipeline failed to process it"
    elif rss_state in (RssAcquisition.GREEN, RssAcquisition.YELLOW):
        coverage = IdentityCoverage.COVERED_BY_RSS
        cov_detail = "acquired through the normal feed"
    elif rss_state is RssAcquisition.RED and acquired_by_sweep:
        # §8, made executable: this is a FEED problem, not a coverage gap.
        coverage = IdentityCoverage.RSS_RED_COVERED_BY_SWEEP
        cov_detail = "feed was late or silent; a complete sweep recovered it"
    elif acquired_by_sweep:
        coverage = IdentityCoverage.COVERED_BY_SWEEP
        cov_detail = "recovered by a complete sweep"
    elif first_sweep:
        # Seen only by a sweep that never proved completion. We cannot claim
        # coverage from an attempt that could not vouch for its own interval.
        coverage = IdentityCoverage.AMBIGUOUS_IDENTITY
        cov_detail = "seen only by a sweep that did not reach completion"
    elif rss_state is RssAcquisition.PENDING:
        # No coverage answer yet — see ItemVerdict.coverage_state. This is the
        # lag rule's honest form: not a miss, and not a claim of acquisition.
        coverage = None
        cov_detail = "inside the acquisition band; not yet judgeable"
    else:
        coverage = IdentityCoverage.AMBIGUOUS_IDENTITY
        cov_detail = "not acquired by any route"

    return ItemVerdict(url, rss_state, coverage, latency,
                       f"{rss_detail}; {cov_detail}")


# ─────────────────────────────── promotion ─────────────────────────────────

@dataclass
class PromotionVerdict:
    ready: bool
    blocking: list = field(default_factory=list)
    satisfied: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.ready


def evaluate_promotion(
    *,
    source_health: dict,
    required_sources,
    item_verdicts,
    all_discoveries_persisted: Optional[bool] = None,
    watermark_advanced_after_partial_persistence: Optional[bool] = None,
    restart_recovery_proven: Optional[bool] = None,
    missed_poll_recovery_proven: Optional[bool] = None,
    incomplete_sweep_recovery_proven: Optional[bool] = None,
    reconciliation_fail_closed: Optional[bool] = None,
    request_floor_met: Optional[bool] = None,
    listing_volume_evidence: Optional[bool] = None,
    auto_grab_enabled: Optional[bool] = None,
    baseline_requests: Optional[int] = None,
    sweep_requests: Optional[int] = None,
) -> PromotionVerdict:
    """The §10 checklist, conjunctive and FAIL-CLOSED.

    Every evidence argument defaults to None meaning "not demonstrated", and None
    blocks exactly as False does. Absent evidence is not passing evidence — a
    gate that treats a missing measurement as satisfied is not a gate.
    """
    blocking: list = []
    satisfied: list = []

    for key in required_sources:
        h: Optional[SourceHealth] = source_health.get(key)
        if h is None:
            blocking.append(f"{key}: no health reading (source not evaluated)")
        elif h.state is not IntervalState.CURRENT:
            blocking.append(f"{key}: interval is {h.state.value} — {h.detail}")
        else:
            satisfied.append(f"{key}: interval current")

    # Items still inside the acquisition band are excluded from the coverage
    # tally and reported separately. They cannot pass (nothing is proven) and
    # cannot fail (nothing is late) — counting them either way would make the
    # verdict a function of when it was run.
    all_items = list(item_verdicts)
    pending = [v for v in all_items if not v.judgeable]
    if pending:
        satisfied.append(f"{len(pending)} item(s) still inside the acquisition band "
                         "— excluded as not yet judgeable")

    verdicts = [v for v in all_items if v.judgeable]
    ambiguous = [v for v in verdicts if v.coverage_state is IdentityCoverage.AMBIGUOUS_IDENTITY]
    if ambiguous:
        blocking.append(
            f"{len(ambiguous)} item(s) with ambiguous identity: "
            + ", ".join(v.canonical_url for v in ambiguous[:3])
        )
    else:
        satisfied.append("no ambiguous identities")

    failed = [v for v in verdicts if v.coverage_state is IdentityCoverage.PROCESSING_FAILED]
    if failed:
        blocking.append(f"{len(failed)} item(s) acquired but not processed")
    else:
        satisfied.append("no processing failures")

    # RED-recovered items are deliberately NOT counted here. They degrade feed
    # health and are surfaced separately; treating them as coverage gaps is the
    # exact conflation this module exists to prevent.
    red_recovered = [v for v in verdicts
                     if v.coverage_state is IdentityCoverage.RSS_RED_COVERED_BY_SWEEP]
    if red_recovered:
        satisfied.append(
            f"{len(red_recovered)} RED item(s) recovered by sweep "
            "(feed-health metric, not a coverage gap)"
        )

    _require(blocking, satisfied, all_discoveries_persisted,
             "all discoveries durably persisted")
    _require(blocking, satisfied, restart_recovery_proven,
             "restart recovery proven")
    _require(blocking, satisfied, missed_poll_recovery_proven,
             "induced missed poll recovered")
    _require(blocking, satisfied, incomplete_sweep_recovery_proven,
             "induced incomplete sweep recovered")
    _require(blocking, satisfied, reconciliation_fail_closed,
             "reconciliation is fail-closed")
    _require(blocking, satisfied, listing_volume_evidence,
             "listing-volume evidence exists")

    # Inverted requirements: these must be demonstrably FALSE. None still blocks,
    # because "nobody checked whether the watermark advanced after a partial
    # write" is not evidence that it did not.
    if watermark_advanced_after_partial_persistence is None:
        blocking.append("NOT DEMONSTRATED: no watermark advance after partial persistence")
    elif watermark_advanced_after_partial_persistence:
        blocking.append("a watermark advanced after partial persistence")
    else:
        satisfied.append("no watermark advance after partial persistence")

    if auto_grab_enabled is None:
        blocking.append("NOT DEMONSTRATED: auto-grab is off")
    elif auto_grab_enabled:
        blocking.append("auto-grab is ENABLED — must be off for promotion")
    else:
        satisfied.append("auto-grab off")

    # Request cost: measured if both counts are given, otherwise falls back to
    # the caller's assertion, and blocks if neither exists.
    if baseline_requests and sweep_requests is not None:
        reduction = 1.0 - (sweep_requests / baseline_requests)
        if reduction < MIN_REQUEST_REDUCTION:
            blocking.append(
                f"request reduction {reduction:.0%} below the {MIN_REQUEST_REDUCTION:.0%} "
                f"floor ({sweep_requests} vs baseline {baseline_requests})"
            )
        else:
            note = "" if reduction >= TARGET_REQUEST_REDUCTION else " (below the 70% target)"
            satisfied.append(f"request reduction {reduction:.0%}{note}")
    else:
        _require(blocking, satisfied, request_floor_met, "request floor met")

    return PromotionVerdict(ready=not blocking, blocking=blocking, satisfied=satisfied)


def _require(blocking, satisfied, value, label) -> None:
    if value is None:
        blocking.append(f"NOT DEMONSTRATED: {label}")
    elif not value:
        blocking.append(f"FAILED: {label}")
    else:
        satisfied.append(label)


def _parse(value) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
