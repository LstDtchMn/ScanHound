"""Round 15 §7-§9: the coverage evaluator, and the fixtures the review demanded.

The architecture ruling was option 2 -- the crawler emits raw ordered traversal
facts, and a SEPARATE versioned evaluator derives the frontier. Recording an
observation is not granting a permission, which is the same separation that made
the listing ledger safe.

Nothing here writes `category_attested`. The closure list was explicit: build the
contract and the evaluator first.

The reviewer named the fixtures, and one as critical:

    "The sticky mutation is critical: an algorithm using min(observed
     posted_date) must fail."

so `TestAStickyPostCannotManufactureAFrontier` is the load-bearing class in this
file. Everything else guards a rule from §8.
"""
import pytest

from backend.coverage import (
    Arm, CoverageEvaluator, Page, Sighting, TraversalReport,
    EVALUATOR_VERSION, parse_site_date,
)

D = {
    "u/aug20a": "August 20, 2026 at 11:00 PM",
    "u/aug20b": "August 20, 2026 at 10:00 PM",
    "u/aug19":  "August 19, 2026 at 9:00 PM",
    "u/aug18":  "August 18, 2026 at 8:00 PM",
    "u/aug17":  "August 17, 2026 at 7:00 PM",
    "u/sticky": "January 4, 2024 at 1:00 AM",
    "u/tie_a":  "August 19, 2026 at 9:00 PM",   # same minute as u/aug19
    "u/nodate": None,
}


def _sights(*urls):
    out = []
    for i, u in enumerate(urls, start=1):
        if isinstance(u, tuple):
            name, kw = u
            out.append(Sighting(position=i, canonical_url=name, **kw))
        else:
            out.append(Sighting(position=i, canonical_url=u))
    return out


def _report(*arms, run="run-1"):
    return TraversalReport(run_id=run, source="hdencode", arms=list(arms))


def _arm(key, ltype, *pages):
    return Arm(arm_key=key, listing_type=ltype, parser_version="p1",
               pages=list(pages))


def _ev(unstable=None):
    return CoverageEvaluator({k: v for k, v in D.items() if v}, unstable)


class TestAStickyPostCannotManufactureAFrontier:
    """THE CRITICAL ONE. The reviewer's counterexample, verbatim in shape."""

    def test_a_pinned_old_post_at_the_bottom_of_page_one(self):
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug20b",
                                             "u/aug19", "u/sticky")))
        v = _ev().evaluate_arm(_report(arm), arm)

        # min(observed posted_date) would be January 2024 -- months of coverage
        # conjured from a single page. The frontier must not be that.
        assert v.proven
        assert v.proof.frontier_date_raw != D["u/sticky"], (
            "the frontier adopted the sticky post's date, which is exactly the "
            "min(posted_date) failure the reviewer rejected")
        assert v.proof.frontier_url == "u/aug19", (
            "the frontier should stop at the last CORROBORATED anchor")

    def test_an_algorithm_using_min_would_disagree_with_us(self):
        """States the defeated alternative explicitly, so a future refactor that
        reintroduces min() has something to fail against."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/sticky")))
        v = _ev().evaluate_arm(_report(arm), arm)
        naive_min = min(parse_site_date(D[u])
                        for u in ("u/aug20a", "u/aug19", "u/sticky"))
        assert v.proof.frontier_date > naive_min, (
            "the evaluator's frontier is no better than min(posted_date)")

    def test_a_sticky_in_the_middle_refuses_outright(self):
        """Followed by newer items, so the ordering is provably not monotonic."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/sticky",
                                             "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "inversion" in v.reason


class TestTheRulesFromSection8:

    def test_a_repeated_url_cannot_anchor(self):
        """§8.4. A repeat proves nothing about NEW depth."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19")),
                   Page(2, sightings=_sights(
                       ("u/aug20a", {"duplicate_in_run": True}), "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_url == "u/aug19", (
            "the repeated page-1 URL was treated as an ordering signal on page 2")

    def test_an_http_200_page_the_parser_cannot_read_stops_the_walk(self):
        """§8.2. Recognition, not merely a status code."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")),
                   Page(2, http_status=200, parser_state="unrecognised"))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "unusable" in v.reason

    def test_a_page_error_before_the_frontier_refuses(self):
        """§8.1. A gap means the traversal was not contiguous."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, page_error="HTTP 502",
                        sightings=_sights("u/aug20a", "u/aug19")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven

    def test_an_unknown_date_does_not_anchor_but_does_not_block(self):
        """§8.6. A policy-excluded full disc has no trustworthy date, and the
        traversal continues past it to a later anchor."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights(
                       "u/aug20a",
                       ("u/nodate", {"policy_excluded": True}),
                       "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_url == "u/aug19"

    def test_a_date_seen_to_change_cannot_anchor(self):
        """§8.7. posted_date_changed=1 cannot support timestamp coverage."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")))
        v = _ev(unstable={"u/aug19"}).evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_url == "u/aug20a", (
            "an unstable date was used as the frontier anchor")

    def test_the_proof_carries_its_versions(self):
        """§8.8. A proof is only meaningful with the logic that produced it."""
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proof.evaluator_version == EVALUATOR_VERSION
        assert v.proof.parser_version == "p1"


class TestCrossingIsTargetRelative:
    """§9. The question is always 'did we get older than R', never 'did we read
    N pages'. A fixed page budget is never evidence by itself."""

    def _both_arms(self):
        return _report(
            _arm("hdencode:4k", "movie",
                 Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug17"))),
            _arm("hdencode:tv", "tv",
                 Page(1, sightings=_sights("u/aug20b", "u/aug19", "u/aug17"))))

    def test_a_release_newer_than_both_frontiers_is_covered(self):
        ok, verdicts, why = _ev().covers_release(
            self._both_arms(), "August 20, 2026 at 11:30 PM")
        assert ok, why

    def test_a_release_older_than_the_frontier_is_not_covered(self):
        ok, _, why = _ev().covers_release(
            self._both_arms(), "August 1, 2026 at 1:00 AM")
        assert not ok
        assert "not traversed past" in why

    def test_equal_minute_does_not_prove_crossing(self):
        """§8.5. Minute resolution: two releases in the same minute are unordered
        with respect to each other, so one cannot vouch for the other."""
        ok, _, why = _ev().covers_release(
            self._both_arms(), D["u/aug19"])       # exactly the frontier
        assert not ok, (
            "an equal timestamp was accepted as proof of crossing; strictly "
            "older is the only thing that proves it")

    def test_a_missing_contradictory_arm_refuses(self):
        movie_only = _report(
            _arm("hdencode:4k", "movie",
                 Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug17"))))
        ok, _, why = _ev().covers_release(
            movie_only, "August 20, 2026 at 11:30 PM")
        assert not ok
        assert "no tv arm" in why

    def test_a_target_without_a_readable_date_refuses(self):
        ok, _, why = _ev().covers_release(self._both_arms(), "sometime last year")
        assert not ok
        assert "readable date" in why


class TestTheValidCase:
    """POSITIVE CONTROL. Without this, an evaluator that refused everything would
    satisfy every assertion above while making attestation impossible forever."""

    def test_a_clean_multi_page_monotonic_traversal_proves_coverage(self):
        arm = _arm("hdencode:4k", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug20b")),
                   Page(2, sightings=_sights("u/aug19", "u/aug18")),
                   Page(3, sightings=_sights("u/aug17")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.pages_traversed == 3
        assert v.proof.anchors_used == 5
        assert v.proof.frontier_url == "u/aug18", (
            "the frontier is the last CORROBORATED anchor -- one short of the "
            "deepest, deliberately")

    def test_the_evaluator_grants_nothing_by_itself(self):
        """It returns a proof object. It does not write attestation, and it has
        no database handle to write one with."""
        ev = _ev()
        assert not hasattr(ev, "db")
        assert not any("attest" in n for n in dir(ev))
