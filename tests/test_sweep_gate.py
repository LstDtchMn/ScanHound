"""The lag-aware gate — feed health and product coverage must not collapse.

Track A's error was one state model answering two questions. These tests hold
the two apart, and the promotion checklist fails closed on absent evidence.
"""

import datetime as dt

import pytest

from backend.sweep.gate import (
    IdentityCoverage,
    RssAcquisition,
    classify_item,
    evaluate_promotion,
)
from backend.sweep.health import IntervalState, SourceHealth

NOW = dt.datetime(2026, 8, 1, 12, 0, 0)


def item(**kw):
    base = {"canonical_url": "https://hdencode.org/x",
            "published_at": NOW - dt.timedelta(hours=3)}
    base.update(kw)
    return base


def hours(n):
    return NOW - dt.timedelta(hours=n)


def cls(**kw):
    return classify_item(item(**kw), now=NOW)


# ───────────────────────────── the FEED axis ────────────────────────────────

class TestRssAcquisition:
    def test_normal_feed_within_six_hours_is_green(self):
        v = cls(published_at=hours(3), first_normal_at=hours(2))
        assert v.rss_state is RssAcquisition.GREEN
        assert v.normal_feed_latency_hours == pytest.approx(1.0)

    def test_six_to_twenty_four_hours_is_yellow(self):
        v = cls(published_at=hours(30), first_normal_at=hours(20))
        assert v.rss_state is RssAcquisition.YELLOW

    def test_beyond_twenty_four_hours_is_red(self):
        v = cls(published_at=hours(60), first_normal_at=hours(20))
        assert v.rss_state is RssAcquisition.RED

    def test_latency_is_measured_from_first_normal_at(self):
        """Round 6 required this explicitly. Measuring from a catch-up or sweep
        observation is what produced the false '0 of 100 acquired'."""
        v = cls(published_at=hours(5), first_normal_at=hours(4),
                first_sweep_at=hours(1))
        assert v.normal_feed_latency_hours == pytest.approx(1.0)
        assert v.rss_state is RssAcquisition.GREEN

    def test_no_publication_time_is_ambiguous_not_green(self):
        v = cls(published_at=None, first_normal_at=hours(1))
        assert v.rss_state is RssAcquisition.AMBIGUOUS


class TestLagAwareness:
    def test_young_unacquired_item_is_pending_not_missed(self):
        """THE LAG RULE. An item 20 minutes old has not been missed by a 6-hour
        band — calling it missed measures the clock, not the pipeline."""
        v = cls(published_at=NOW - dt.timedelta(minutes=20), first_normal_at=None)
        assert v.rss_state is RssAcquisition.PENDING
        assert not v.is_gap

    def test_pending_makes_no_coverage_claim_in_EITHER_direction(self):
        """It is not a miss, and it is also not an acquisition. Asserting either
        would state a fact that has not happened yet."""
        v = cls(published_at=NOW - dt.timedelta(minutes=20), first_normal_at=None)
        assert v.coverage_state is None
        assert not v.judgeable
        assert not v.is_covered and not v.is_gap

    def test_pending_becomes_yellow_once_the_band_expires(self):
        v = cls(published_at=hours(8), first_normal_at=None)
        assert v.rss_state is RssAcquisition.YELLOW

    def test_the_real_track_a_shape_scores_green(self):
        """~1 h observation lag on a normal feed. This is what 99 of the 100
        'shadow misses' actually were, and it must not read as a problem."""
        v = cls(published_at=hours(2), first_normal_at=hours(2) + dt.timedelta(minutes=61))
        assert v.rss_state is RssAcquisition.GREEN
        assert v.coverage_state is IdentityCoverage.COVERED_BY_RSS
        assert not v.degrades_feed_health


# ──────────────────────────── the PRODUCT axis ──────────────────────────────

class TestIdentityCoverage:
    def test_red_recovered_by_sweep_is_covered_not_a_gap(self):
        """§8 verbatim: 'a RED RSS item recovered by a complete sweep is an
        RSS-health metric, not an uncovered release.'"""
        v = cls(published_at=hours(60), first_normal_at=None,
                first_sweep_at=hours(1), sweep_complete=True)
        assert v.rss_state is RssAcquisition.RED
        assert v.coverage_state is IdentityCoverage.RSS_RED_COVERED_BY_SWEEP
        assert v.is_covered                 # the product is fine
        assert v.degrades_feed_health       # the feed is not

    def test_the_two_axes_disagree_and_that_is_correct(self):
        """The single most important property of this module: one item, bad on
        one axis and good on the other, with neither answer overwriting the
        other."""
        v = cls(published_at=hours(60), first_normal_at=None,
                first_sweep_at=hours(1), sweep_complete=True)
        assert v.degrades_feed_health and v.is_covered

    def test_sweep_recovery_requires_a_COMPLETE_sweep(self):
        """An incomplete sweep cannot vouch for its own interval, so it cannot
        confer coverage — the same conjunction rule as completion.py."""
        v = cls(published_at=hours(60), first_normal_at=None,
                first_sweep_at=hours(1), sweep_complete=False)
        assert v.coverage_state is IdentityCoverage.AMBIGUOUS_IDENTITY
        assert not v.is_covered

    def test_unresolvable_identity_short_circuits_both_axes(self):
        v = cls(identity_ambiguous=True, first_normal_at=hours(1))
        assert v.rss_state is RssAcquisition.AMBIGUOUS
        assert v.coverage_state is IdentityCoverage.AMBIGUOUS_IDENTITY

    def test_acquired_but_unprocessed_is_not_covered(self):
        v = cls(first_normal_at=hours(2), processing_failed=True)
        assert v.coverage_state is IdentityCoverage.PROCESSING_FAILED
        assert not v.is_covered

    def test_never_acquired_by_any_route_is_a_real_gap(self):
        v = cls(published_at=hours(60), first_normal_at=None, first_sweep_at=None)
        assert v.rss_state is RssAcquisition.RED
        assert not v.is_covered


# ────────────────────────────── promotion ───────────────────────────────────

def health(state=IntervalState.CURRENT):
    return SourceHealth(
        source_key="4k_movies", state=state, coverage_through=hours(1),
        due_at=NOW, overdue_at=NOW, coverage_age_hours=1.0,
        is_overdue=False, is_running=False, is_incomplete=False,
        is_degraded=False, consecutive_failures=0, detail="ok",
    )


ALL_EVIDENCE = dict(
    all_discoveries_persisted=True,
    watermark_advanced_after_partial_persistence=False,
    restart_recovery_proven=True,
    missed_poll_recovery_proven=True,
    incomplete_sweep_recovery_proven=True,
    reconciliation_fail_closed=True,
    request_floor_met=True,
    listing_volume_evidence=True,
    auto_grab_enabled=False,
)


def promo(**over):
    kw = dict(source_health={"4k_movies": health()}, required_sources=["4k_movies"],
              item_verdicts=[], **ALL_EVIDENCE)
    kw.update(over)
    return evaluate_promotion(**kw)


class TestPromotionFailsClosed:
    def test_full_evidence_passes(self):
        assert promo().ready, promo().blocking

    @pytest.mark.parametrize("field", sorted(ALL_EVIDENCE))
    def test_every_missing_measurement_blocks(self, field):
        """FAIL-CLOSED. Absent evidence is not passing evidence — a gate that
        treats an unmeasured requirement as satisfied is not a gate."""
        v = promo(**{field: None})
        assert v.blocked
        assert any("NOT DEMONSTRATED" in b for b in v.blocking)

    def test_auto_grab_on_blocks_promotion(self):
        v = promo(auto_grab_enabled=True)
        assert v.blocked
        assert any("auto-grab is ENABLED" in b for b in v.blocking)

    def test_a_watermark_advance_after_partial_persistence_blocks(self):
        v = promo(watermark_advanced_after_partial_persistence=True)
        assert v.blocked

    def test_unevaluated_source_blocks_rather_than_passing_silently(self):
        v = promo(source_health={}, required_sources=["4k_movies", "tv_packs"])
        assert v.blocked
        assert len([b for b in v.blocking if "no health reading" in b]) == 2

    @pytest.mark.parametrize("state", [
        IntervalState.DUE, IntervalState.OVERDUE, IntervalState.RUNNING,
        IntervalState.RUNNING_OVERDUE, IntervalState.INCOMPLETE,
        IntervalState.INCOMPLETE_OVERDUE, IntervalState.DEGRADED,
        IntervalState.UNKNOWN,
    ])
    def test_any_non_current_interval_blocks(self, state):
        assert promo(source_health={"4k_movies": health(state)}).blocked


class TestPromotionCoverageRules:
    def test_red_recovered_items_do_NOT_block(self):
        """The §8 rule at the gate level: recovered RED items are feed health,
        so a source with many of them can still promote on coverage."""
        v = promo(item_verdicts=[cls(published_at=hours(60), first_normal_at=None,
                                     first_sweep_at=hours(1), sweep_complete=True)])
        assert v.ready, v.blocking
        assert any("feed-health metric" in s for s in v.satisfied)

    def test_ambiguous_identity_blocks(self):
        v = promo(item_verdicts=[cls(identity_ambiguous=True)])
        assert v.blocked
        assert any("ambiguous identity" in b for b in v.blocking)

    def test_processing_failure_blocks(self):
        v = promo(item_verdicts=[cls(first_normal_at=hours(1), processing_failed=True)])
        assert v.blocked

    def test_pending_items_neither_block_nor_silently_vanish(self):
        """Counting them either way would make the verdict depend on when it was
        run rather than on how the pipeline performed."""
        v = promo(item_verdicts=[
            cls(published_at=NOW - dt.timedelta(minutes=10), first_normal_at=None)])
        assert v.ready, v.blocking
        assert any("not yet judgeable" in s for s in v.satisfied)

    def test_a_real_gap_among_pending_items_still_blocks(self):
        """The exclusion must not become a hiding place."""
        v = promo(item_verdicts=[
            cls(published_at=NOW - dt.timedelta(minutes=10), first_normal_at=None),
            cls(published_at=hours(60), first_normal_at=None, first_sweep_at=None),
        ])
        assert v.blocked


class TestRequestCost:
    def test_measured_reduction_below_the_floor_blocks(self):
        """§10: 'Below 50% fails promotion regardless of coverage.'"""
        v = promo(baseline_requests=100, sweep_requests=60, request_floor_met=True)
        assert v.blocked
        assert any("below the 50% floor" in b for b in v.blocking)

    def test_measured_reduction_above_the_floor_passes(self):
        v = promo(baseline_requests=100, sweep_requests=40, request_floor_met=None)
        assert v.ready, v.blocking

    def test_between_floor_and_target_passes_but_is_noted(self):
        v = promo(baseline_requests=100, sweep_requests=45, request_floor_met=None)
        assert v.ready
        assert any("below the 70% target" in s for s in v.satisfied)

    def test_measurement_overrides_a_caller_assertion(self):
        """A measured failure must beat an asserted pass — otherwise the flag
        becomes a way to talk past the numbers."""
        v = promo(baseline_requests=100, sweep_requests=95, request_floor_met=True)
        assert v.blocked
