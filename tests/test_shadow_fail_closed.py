"""Shadow reconciliation must fail CLOSED (§10).

Every case here previously returned `success` — a clean verdict reached with no
usable evidence. A reconciliation that passes because it had nothing to compare
is not a check.
"""

import pytest

from backend.hdencode_shadow import INCONCLUSIVE_OUTCOMES, compare_shadow


def cmp(rss, listing, *, complete=True, rss_req=2, listing_req=5):
    return compare_shadow(rss_urls=rss, listing_items=listing,
                          rss_requests=rss_req, listing_requests=listing_req,
                          normal_feeds_complete=complete)


A = "https://hdencode.org/a/"
A_NOSLASH = "https://hdencode.org/a"
B = "https://hdencode.org/b/"


class TestOrdinaryOutcomesStillWork:
    def test_matching_sets_succeed(self):
        r = cmp([A], [{"url": A_NOSLASH, "status": "in_library"}])
        assert r.outcome == "success"
        assert r.qualifies and r.is_conclusive

    def test_a_relevant_miss_is_still_reported(self):
        r = cmp([A], [{"url": A_NOSLASH, "status": "in_library"},
                      {"url": B, "status": "dv_upgrade", "title": "B"}])
        assert r.outcome == "relevant_miss"
        assert r.relevant_miss_count == 1
        assert not r.qualifies

    def test_incomplete_feeds_still_reported(self):
        r = cmp([A], [{"url": A_NOSLASH, "status": "in_library"}], complete=False)
        assert r.outcome == "incomplete_feeds"

    def test_canonicalisation_still_matches_across_slash_variants(self):
        """One canonicaliser on both sides — the property the fail-closed guard
        below exists to detect the absence of."""
        assert cmp([A], [{"url": A_NOSLASH, "status": "in_library"}]).duplicate_count == 1


class TestFailClosed:
    def test_empty_listing_is_NOT_success(self):
        """No baseline to detect misses against. Zero misses means zero
        comparisons were possible, not zero problems."""
        r = cmp([A, B], [])
        assert r.outcome == "no_listing_baseline"
        assert not r.qualifies and not r.is_conclusive

    def test_empty_rss_is_NOT_success(self):
        """The nastiest of the three: RSS returned nothing, and because no
        listing item happened to be in a relevant state, the miss list was empty
        and the cycle scored a clean pass on a dead feed."""
        r = cmp([], [{"url": A, "status": "in_library"}])
        assert r.outcome == "no_rss_observations"
        assert not r.qualifies

    def test_both_empty_is_NOT_success(self):
        assert cmp([], []).outcome == "no_listing_baseline"

    def test_zero_overlap_is_flagged_as_an_identity_mismatch(self):
        """THE INCIDENT SIGNATURE. Two views of the same source overlapping in
        nothing is a broken join, not genuine divergence. This is what the two
        trailing-slash-divergent canonicalisers produced when a 99-of-100
        pipeline was reported as '0 of 100 never acquired'."""
        r = cmp(["https://hdencode.org/x/", "https://hdencode.org/y/"],
                [{"url": "https://hdencode.org/p", "status": "in_library"},
                 {"url": "https://hdencode.org/q", "status": "in_library"}])
        assert r.outcome == "disjoint_identity_sets"
        assert not r.is_conclusive

    def test_a_single_overlap_is_enough_to_clear_the_disjoint_guard(self):
        """The guard targets total non-overlap, not partial divergence — normal
        cycles legitimately differ at the edges."""
        r = cmp([A, "https://hdencode.org/x/"],
                [{"url": A_NOSLASH, "status": "in_library"}])
        assert r.outcome == "success"

    @pytest.mark.parametrize("outcome", sorted(INCONCLUSIVE_OUTCOMES))
    def test_no_inconclusive_outcome_counts_as_qualifying(self, outcome):
        assert outcome != "success"


class TestGuardsOverrideOptimisticVerdicts:
    def test_empty_listing_overrides_success_even_with_complete_feeds(self):
        assert cmp([A], [], complete=True).outcome == "no_listing_baseline"

    def test_guards_do_not_suppress_a_real_miss_when_evidence_exists(self):
        """The guards must not become a way to discard findings — with a real
        baseline and a real overlap, a miss still wins."""
        r = cmp([A], [{"url": A_NOSLASH, "status": "in_library"},
                      {"url": B, "status": "missing", "title": "B"}])
        assert r.outcome == "relevant_miss"

    def test_request_reduction_is_still_computed_for_blocked_cycles(self):
        """An inconclusive cycle must still report its numbers — suppressing
        them would hide the cost of a broken cycle."""
        r = cmp([], [], rss_req=2, listing_req=10)
        assert r.request_reduction_pct == 80.0
