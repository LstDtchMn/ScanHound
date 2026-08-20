"""Guard outcomes: what they outrank, and what they may not be used as.

Peer review round 11 found two defects in how the hybrid-sweep merge integrated
the three fail-closed outcomes. Both were mine, and both were introduced by a
fix for an earlier round of the same integration.

I1 -- I applied all three guards only when the outcome would otherwise be
'success', reasoning from their own comment ("each of these previously produced
'success'"). That is right for two of them and wrong for the third:

    no_listing_baseline / no_rss_observations
        "this cycle saw nothing to compare" -- guards a false CLEAN.
        A relevant_miss is a real observation and outranks them.

    disjoint_identity_sets
        "the JOIN between the two sides is unusable" -- which invalidates the
        comparison itself, INCLUDING any miss derived from it. A broken join
        manufactures listing_only rows; if one carries a relevant state the
        outcome became relevant_miss and the guard never ran, persisting a
        broken join as real miss evidence.

I2 -- admitting the guard outcomes into the miss resolver (so their
unattributed candidates keep blocking) also made them ORDINARY OBSERVATIONS,
because cycle_is_valid_evidence_for() checks listing_complete and per-feed
outcomes and never looked at `outcome`. So a guard cycle could clear a
candidate and resolve a prior miss. The comment I wrote claimed they "can only
continue to BLOCK". The code did not implement that.
"""
from __future__ import annotations

import datetime as dt

import pytest

from backend.hdencode_shadow import (
    INCONCLUSIVE_OUTCOMES,
    classify_miss_resolution,
    compare_shadow,
    cycle_is_valid_evidence_for,
)

BOTH_OK = {"movies_all": "changed", "tv_all": "changed"}


class _Item:
    def __init__(self, url, status="missing", title="T"):
        self.canonical_url = url
        self.url = url
        self.status = status
        self.title = title

    def get(self, k, d=None):
        return getattr(self, k, d)


def _cmp(*, rss, listing, outcomes=None, complete=True):
    return compare_shadow(
        rss_urls=list(rss),
        listing_items=list(listing),
        rss_requests=2, listing_requests=7,
        normal_feeds_complete=complete,
        normal_feed_outcomes=outcomes if outcomes is not None else BOTH_OK,
        listing_complete=True,
        raw_listing_urls=[i.canonical_url for i in listing])


X = "https://hdencode.org/x-2026-2160p-web-2-2-gb"
Y = "https://hdencode.org/y-2026-2160p-web-3-3-gb"
P = "https://hdencode.org/p-2026-2160p-web-4-4-gb"
Q = "https://hdencode.org/q-2026-2160p-web-5-5-gb"

#: Zero overlap only means something once overlap was EXPECTED, so the disjoint
#: fixtures must clear _DISJOINT_MIN_IDENTITIES on both sides. Live cycles carry
#: rss_count=100 and listing_count~56, so a realistic fixture is not two items.
BULK_RSS = [f"https://hdencode.org/rss-{i}-2026-1080p-web-{i}-0-gb" for i in range(8)]
BULK_LISTING = [f"https://hdencode.org/lst-{i}-2026-1080p-web-{i}-0-gb" for i in range(8)]


class TestI1GuardPrecedence:
    def test_a_broken_join_is_not_persisted_as_a_real_miss(self):
        """THE DEFECT. Zero overlap AND a relevant listing row.

        The existing zero-overlap test could not catch this: its listing rows
        are all in_library, so `misses` stayed empty and the pre-guard outcome
        stayed 'success', which the guard did cover.
        """
        r = _cmp(rss=BULK_RSS,
                 listing=[_Item(u, "in_library") for u in BULK_LISTING[:-1]]
                         + [_Item(BULK_LISTING[-1], "missing")])
        assert r.outcome == "disjoint_identity_sets", (
            "a miss derived from an unusable identity join was certified as "
            "real miss evidence")

    def test_zero_overlap_with_no_relevant_row_still_guards(self):
        """The case that already worked -- kept so the fix cannot regress it."""
        r = _cmp(rss=BULK_RSS,
                 listing=[_Item(u, "in_library") for u in BULK_LISTING])
        assert r.outcome == "disjoint_identity_sets"

    def test_a_real_miss_with_a_working_join_is_still_a_miss(self):
        """POSITIVE CONTROL. Without this, 'always disjoint' would pass above.

        Overlap exists, so the join is usable and a relevant listing-only row
        is a genuine observation.
        """
        r = _cmp(rss=BULK_RSS,
                 listing=[_Item(u, "in_library") for u in BULK_RSS[:6]]
                         + [_Item(P, "missing")])
        assert r.outcome == "relevant_miss"

    def test_a_small_cycle_with_no_overlap_is_an_ordinary_miss(self):
        """The case that made the first version of this fix wrong.

        A one-item feed and a two-item listing sharing nothing is just RSS not
        having those releases yet -- the ordinary miss this subsystem exists to
        record, not a broken join.
        """
        r = _cmp(rss=[X], listing=[_Item(P, "missing"), _Item(Q, "missing")])
        assert r.outcome == "relevant_miss"

    def test_an_empty_feed_does_not_outrank_a_real_miss(self):
        """The other two guards keep the narrower precedence: a relevant_miss
        is an observation and outranks 'we saw nothing to compare'."""
        r = _cmp(rss=[], listing=[_Item(P, "missing")])
        assert r.outcome == "relevant_miss", (
            "no_rss_observations must not overwrite a real observation")


class TestI2InconclusiveIsNotAnObservation:
    """A guard cycle may BLOCK. It may not CLEAR or RESOLVE."""

    def _cycle(self, outcome, *, feed_only=(), listing_only=(), duplicates=()):
        return {"at": dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
                "feed_only": tuple(feed_only),
                "listing_only": tuple(listing_only),
                "duplicate_urls": tuple(duplicates),
                "outcomes": BOTH_OK,
                "listing_complete": True,
                "cycle_complete": True,
                "outcome": outcome}

    @pytest.mark.parametrize("outcome", sorted(INCONCLUSIVE_OUTCOMES))
    def test_a_guard_cycle_is_not_valid_evidence(self, outcome):
        assert cycle_is_valid_evidence_for("movie", self._cycle(outcome)) is False

    @pytest.mark.parametrize("outcome", ["success", "relevant_miss",
                                         "incomplete_feeds"])
    def test_an_ordinary_cycle_still_is(self, outcome):
        """POSITIVE CONTROL, and it protects the fix this merge was making:
        incomplete_feeds is NOT one of the guard outcomes and must keep
        supporting the healthy feed when the other one failed."""
        assert cycle_is_valid_evidence_for("movie", self._cycle(outcome)) is True

    def test_a_disjoint_cycle_cannot_prove_a_release_is_still_missing(self):
        """Requirement A. listing_only from an unusable join is not proof."""
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        state, _hours, _detail = classify_miss_resolution(
            P, "movie", first_seen,
            [self._cycle("disjoint_identity_sets", listing_only=[P])])
        assert state == "not_yet_assessable", (
            "a broken identity join was used as negative evidence")

    def test_a_disjoint_cycle_cannot_prove_acquisition_either(self):
        """Requirement B, and the conservative half. Positive carriage from an
        unusable join may be salvageable later, but that needs two predicates
        rather than one authority bit -- so for now it proves nothing."""
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        state, _h, _d = classify_miss_resolution(
            P, "movie", first_seen,
            [self._cycle("disjoint_identity_sets", feed_only=[P])])
        assert state == "not_yet_assessable"

    def test_an_ordinary_cycle_still_resolves_both_ways(self):
        """POSITIVE CONTROL for the above. Without it, a predicate that
        rejected EVERY cycle would satisfy both tests."""
        first_seen = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        acquired, _h, _d = classify_miss_resolution(
            P, "movie", first_seen, [self._cycle("success", feed_only=[P])])
        assert acquired == "acquired"

        missing, _h2, _d2 = classify_miss_resolution(
            P, "movie", first_seen,
            [self._cycle("relevant_miss", listing_only=[P])])
        assert missing == "never_acquired"

    def test_incomplete_feeds_keeps_per_feed_precision(self):
        """Requirement C. The behaviour this merge was FIXING must survive:
        a cycle where movies_all validated and tv_all failed is good evidence
        about a movie and useless about TV."""
        c = self._cycle("incomplete_feeds")
        c["outcomes"] = {"movies_all": "changed", "tv_all": "failed"}
        c["cycle_complete"] = False
        assert cycle_is_valid_evidence_for("movie", c) is True
        assert cycle_is_valid_evidence_for("tv", c) is False
