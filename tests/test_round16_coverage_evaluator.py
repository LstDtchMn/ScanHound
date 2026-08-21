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
    EVALUATOR_VERSION, parse_site_date, ORDERING_CONTRACTS,
)

D = {
    "u/aug20a": "August 20, 2026 at 11:00 PM",
    "u/aug20b": "August 20, 2026 at 10:00 PM",
    "u/aug19":  "August 19, 2026 at 9:00 PM",
    "u/aug18":  "August 18, 2026 at 8:00 PM",
    "u/aug17":  "August 17, 2026 at 7:00 PM",
    "u/sticky": "January 4, 2024 at 1:00 AM",
    "u/sticky2": "December 2, 2023 at 1:00 AM",
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
        arm = _arm("hdencode:4k:2160p", "movie",
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
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/sticky")))
        v = _ev().evaluate_arm(_report(arm), arm)
        naive_min = min(parse_site_date(D[u])
                        for u in ("u/aug20a", "u/aug19", "u/sticky"))
        assert v.proof.frontier_date > naive_min, (
            "the evaluator's frontier is no better than min(posted_date)")

    def test_a_sticky_in_the_middle_refuses_outright(self):
        """Followed by newer items, so the ordering is provably not monotonic."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/sticky",
                                             "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "inversion" in v.reason


class TestTheRulesFromSection8:

    def test_a_repeated_url_cannot_anchor(self):
        """§8.4. A repeat proves nothing about NEW depth."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19")),
                   Page(2, sightings=_sights(
                       ("u/aug20a", {"duplicate_in_run": True}), "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_url == "u/aug19", (
            "the repeated page-1 URL was treated as an ordering signal on page 2")

    def test_an_http_200_page_the_parser_cannot_read_stops_the_walk(self):
        """§8.2. Recognition, not merely a status code."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")),
                   Page(2, http_status=200, parser_state="unrecognised"))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "unusable" in v.reason

    def test_a_page_error_before_the_frontier_refuses(self):
        """§8.1. A gap means the traversal was not contiguous."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, page_error="HTTP 502",
                        sightings=_sights("u/aug20a", "u/aug19")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven

    def test_an_unknown_date_does_not_anchor_but_does_not_block(self):
        """§8.6. A policy-excluded full disc has no trustworthy date, and the
        traversal continues past it to a later anchor."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights(
                       "u/aug20a",
                       ("u/nodate", {"policy_excluded": True}),
                       "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_url == "u/aug19"

    def test_a_date_seen_to_change_cannot_anchor(self):
        """§8.7. posted_date_changed=1 cannot support timestamp coverage."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")))
        v = _ev(unstable={"u/aug19"}).evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_url == "u/aug20a", (
            "an unstable date was used as the frontier anchor")

    def test_the_proof_carries_its_versions(self):
        """§8.8. A proof is only meaningful with the logic that produced it."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proof.evaluator_version == EVALUATOR_VERSION
        assert v.proof.parser_version == "p1"


class TestATimestampFrontierIsTelemetryNotAuthority:
    """M17-1. Ordering checks defeat ONE terminal anomaly; no fixed count
    defeats k+1. So a frontier is a negative proof only where the SOURCE
    guarantees a chronological stream, and ORDERING_CONTRACTS is empty."""

    def test_two_terminal_outliers_defeat_corroboration(self):
        """THE COUNTEREXAMPLE. Monotonically non-increasing, so no inversion
        fires -- and sticky B corroborates sticky A."""
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19",
                                             "u/sticky", "u/sticky2")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_date_raw == D["u/sticky"], (
            "this documents the DEFEAT: corroboration alone accepts the first "
            "sticky once a second one follows it")
        assert v.proof.authoritative is False, (
            "a frontier defeated by two terminal outliers must not be able to "
            "authorise anything")

    def test_no_proof_is_authoritative_without_a_source_contract(self):
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven
        assert v.proof.authoritative is False
        assert v.proof.ordering_contract == ""

    def test_covers_release_refuses_telemetry(self):
        ok, _, why = _ev().covers_release(
            self._both(), "August 20, 2026 at 11:30 PM",
            ["hdencode:4k:2160p", "hdencode:tv:tv-packs"])
        assert not ok
        assert "ordering contract" in why

    def _both(self):
        return _report(
            _arm("hdencode:4k:2160p", "movie",
                 Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug17"))),
            _arm("hdencode:tv:tv-packs", "tv",
                 Page(1, sightings=_sights("u/aug20b", "u/aug19", "u/aug17"))))


class TestOneCanonicalPostUnderTwoRawAliases:
    """M17-1, the reachable variant. The sighting's identity and date lookup are
    canonical, so a terminal post seen under two cosmetic raw URLs used to give
    two eligible anchors with the SAME date -- and the second corroborated the
    first."""

    def test_an_aliased_repeat_cannot_corroborate(self):
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=[
                       Sighting(1, "u/aug20a"),
                       Sighting(2, "u/aug19"),
                       Sighting(3, "u/sticky", raw_url="/old/"),
                       Sighting(4, "u/sticky", raw_url="/old/?utm=x",
                                duplicate_in_run=True),
                   ]))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason
        assert v.proof.frontier_date_raw != D["u/sticky"], (
            "the aliased repeat corroborated the sticky it is a copy of")
        assert v.proof.frontier_url == "u/aug19"


class TestCrossingRequiresEVERYNamedArm:
    """M17-3. This was existential over TYPES; HDEncode has two movie arms, so a
    deep 4K traversal satisfied 'movie' while a contradiction could sit
    untraversed in Remux."""

    def _three_arms(self, remux_pages):
        return _report(
            _arm("hdencode:4k:2160p", "movie",
                 Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug17"))),
            _arm("hdencode:remux:remux", "movie", *remux_pages),
            _arm("hdencode:tv:tv-packs", "tv",
                 Page(1, sightings=_sights("u/aug20b", "u/aug19", "u/aug17"))))

    REQUIRED = ["hdencode:4k:2160p", "hdencode:remux:remux",
                "hdencode:tv:tv-packs"]

    def _ev_contracted(self):
        """Grant hdencode a contract so this class can test the ARM logic rather
        than re-testing the telemetry gate."""
        import backend.coverage as cov
        ev = _ev()
        cov.ORDERING_CONTRACTS["hdencode"] = "test-contract/1"
        return ev, cov

    def test_a_shallow_same_type_arm_refuses(self, monkeypatch):
        ev, cov = self._ev_contracted()
        try:
            # ONE anchor: nothing corroborates it, so this arm has no usable
            # frontier at all. (An earlier version of this fixture used two
            # recent anchors, which still crossed the target -- "shallow" has
            # to mean shallower than the target, not merely fewer entries.)
            rep = self._three_arms([Page(1, sightings=_sights("u/aug20a"))])
            ok, _, why = ev.covers_release(
                rep, "August 20, 2026 at 11:30 PM", self.REQUIRED)
            assert not ok, "a shallow Remux arm was accepted because 4K crossed"
            assert "remux" in why
        finally:
            cov.ORDERING_CONTRACTS.pop("hdencode", None)

    def test_an_unusable_same_type_arm_refuses(self):
        ev, cov = self._ev_contracted()
        try:
            rep = self._three_arms([Page(1, parser_state="unrecognised")])
            ok, _, why = ev.covers_release(
                rep, "August 20, 2026 at 11:30 PM", self.REQUIRED)
            assert not ok
        finally:
            cov.ORDERING_CONTRACTS.pop("hdencode", None)

    def test_a_required_arm_absent_entirely_refuses(self):
        ev, cov = self._ev_contracted()
        try:
            rep = _report(
                _arm("hdencode:4k:2160p", "movie",
                     Page(1, sightings=_sights("u/aug20a", "u/aug19", "u/aug17"))))
            ok, _, why = ev.covers_release(
                rep, "August 20, 2026 at 11:30 PM", self.REQUIRED)
            assert not ok
            assert "not traversed at all" in why
        finally:
            cov.ORDERING_CONTRACTS.pop("hdencode", None)

    def test_all_required_arms_crossing_passes(self):
        """POSITIVE CONTROL. Without it, a universally-refusing implementation
        would satisfy every assertion above."""
        ev, cov = self._ev_contracted()
        try:
            rep = self._three_arms([Page(1, sightings=_sights(
                "u/aug20a", "u/aug19", "u/aug17"))])
            ok, _, why = ev.covers_release(
                rep, "August 20, 2026 at 11:30 PM", self.REQUIRED)
            assert ok, why
        finally:
            cov.ORDERING_CONTRACTS.pop("hdencode", None)

    def test_an_empty_required_set_is_not_vacuously_true(self):
        ev, cov = self._ev_contracted()
        try:
            ok, _, why = ev.covers_release(
                self._three_arms([Page(1, sightings=_sights("u/aug19"))]),
                "August 20, 2026 at 11:30 PM", [])
            assert not ok
            assert "no required arms" in why
        finally:
            cov.ORDERING_CONTRACTS.pop("hdencode", None)


class TestPageContinuity:
    """M17-2. The evaluator sorted what it was handed and never noticed an
    ABSENT page. The crawler's generic exception path emits no Page at all, so
    [1, 3] is reachable and the walk bridged the gap."""

    def test_a_missing_page_refuses(self):
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a", "u/aug20b")),
                   Page(3, sightings=_sights("u/aug18", "u/aug17")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "page gap" in v.reason and "[2]" in v.reason

    def test_not_starting_at_page_one_refuses(self):
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(2, sightings=_sights("u/aug19", "u/aug18")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "does not start at page 1" in v.reason

    def test_duplicate_page_numbers_refuse(self):
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=_sights("u/aug20a")),
                   Page(1, sightings=_sights("u/aug19")))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "duplicate page numbers" in v.reason

    def test_duplicate_positions_within_a_page_refuse(self):
        arm = _arm("hdencode:4k:2160p", "movie",
                   Page(1, sightings=[Sighting(1, "u/aug20a"),
                                      Sighting(1, "u/aug19")]))
        v = _ev().evaluate_arm(_report(arm), arm)
        assert not v.proven
        assert "duplicate positions" in v.reason


class TestTheValidCase:
    """POSITIVE CONTROL. Without this, an evaluator that refused everything would
    satisfy every assertion above while making attestation impossible forever."""

    def test_a_clean_multi_page_monotonic_traversal_proves_coverage(self):
        arm = _arm("hdencode:4k:2160p", "movie",
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
