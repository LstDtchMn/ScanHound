"""Round 18: M18-1 through M18-4.

Four findings that share one root: a proof was being assembled from parts whose
SCOPE was wider than the claim it supported. A contract for one feed spoke for
its siblings; a duplicate set for one arm spoke for the whole crawl; a page
spoke for posts it had not read; and the date evidence could speak differently
after the fact.

Every class here carries its opposite. A universally-refusing evaluator would
satisfy the refusal assertions and make attestation impossible forever, so each
refusal is paired with a case that MUST still succeed.
"""
import pytest

import backend.coverage as cov
from backend.coverage import (Arm, CoverageEvaluator, CoverageEvidenceSnapshot,
                              Page, Sighting, TraversalReport)
from tests.test_round16_traversal_emission import (_Scraper, _crawl,
                                                   _listing, _source)

# Two arms, one shared release. The 4K arm sees it first.
SHARED = "https://hdencode.example/shared-film-2026/"
ONLY4K = "https://hdencode.example/only-4k-2026/"
OLDER = "https://hdencode.example/older-film-2026/"

PROD_DATES = {
    "https://hdencode.example/shared-film-2026": "August 19, 2026 at 9:00 PM",
    "https://hdencode.example/only-4k-2026":     "August 20, 2026 at 11:00 PM",
    "https://hdencode.example/older-film-2026":  "August 18, 2026 at 8:00 PM",
}

D = {"u/aug20": "August 20, 2026 at 11:00 PM",
     "u/aug19": "August 19, 2026 at 9:00 PM",
     "u/aug18": "August 18, 2026 at 8:00 PM"}


def _sights(*urls):
    return [Sighting(position=i, canonical_url=u)
            for i, u in enumerate(urls, start=1)]


def _arm(key, ltype, *pages, parser="p1", rdv=""):
    return Arm(arm_key=key, listing_type=ltype, parser_version=parser,
               request_definition_version=rdv, pages=list(pages))


def _report(*arms):
    return TraversalReport(run_id="run-18", source="hdencode", arms=list(arms))


@pytest.fixture(autouse=True)
def _no_contracts():
    """ORDERING_CONTRACTS is module-global. Restore it, or one test grants
    authority to every test that runs after it."""
    saved = dict(cov.ORDERING_CONTRACTS)
    cov.ORDERING_CONTRACTS.clear()
    yield
    cov.ORDERING_CONTRACTS.clear()
    cov.ORDERING_CONTRACTS.update(saved)


def _monotonic_arm(key, ltype, parser="p1"):
    """An arm that reaches a corroborated frontier on its own merits."""
    return _arm(key, ltype,
                Page(1, sightings=_sights("u/aug20", "u/aug19", "u/aug18")),
                parser=parser)


# --------------------------------------------------------------------------
# M18-1  a contract is for ONE arm read by ONE parser
# --------------------------------------------------------------------------

class TestAContractDoesNotTransfer:
    """Gate item 7. `ORDERING_CONTRACTS` was keyed on `source`, so establishing
    that ONE HDEncode endpoint publishes chronologically would have marked 4K,
    Remux and TV Packs authoritative together -- three separate empirical
    claims minted from one piece of evidence."""

    TARGET = "August 21, 2026 at 1:00 AM"

    def _verdict(self, key, parser="p1"):
        arm = _monotonic_arm(key, "movie", parser=parser)
        return CoverageEvaluator(D).evaluate_arm(_report(arm), arm)

    def test_the_arm_that_holds_the_contract_is_authoritative(self):
        """The positive control. Without this the class below is vacuous --
        an evaluator that never authorises anything would pass it all."""
        cov.ORDERING_CONTRACTS[("hdencode:4k:2160p", "", "p1")] = "hde-4k/1"
        v = self._verdict("hdencode:4k:2160p")
        assert v.proven, v.reason
        assert v.proof.authoritative
        assert v.proof.ordering_contract == "hde-4k/1"

    def test_a_sibling_arm_of_the_same_source_is_not(self):
        cov.ORDERING_CONTRACTS[("hdencode:4k:2160p", "", "p1")] = "hde-4k/1"
        v = self._verdict("hdencode:remux:remux")
        assert v.proven, "the sibling should still MEASURE a frontier"
        assert not v.proof.authoritative, (
            "a contract for the 4K endpoint authorised the Remux endpoint: "
            "one measurement minted a claim about a feed nobody examined")
        assert v.proof.ordering_contract == ""

    def test_the_same_arm_under_a_different_parser_is_not(self):
        """A parser rewrite can change what listing order MEANS. The contract
        was evidence about a feed AS READ; it does not survive the reader
        changing underneath it."""
        cov.ORDERING_CONTRACTS[("hdencode:4k:2160p", "", "p1")] = "hde-4k/1"
        v = self._verdict("hdencode:4k:2160p", parser="p2")
        assert v.proven
        assert not v.proof.authoritative
        assert v.proof.parser_version == "p2"

    def test_covers_release_names_the_arm_and_parser_that_lack_a_contract(self):
        arms = [_monotonic_arm("hdencode:4k:2160p", "movie"),
                _monotonic_arm("hdencode:remux:remux", "movie")]
        cov.ORDERING_CONTRACTS[("hdencode:4k:2160p", "", "p1")] = "hde-4k/1"
        ok, _, why = CoverageEvaluator(D).covers_release(
            _report(*arms), self.TARGET,
            ["hdencode:4k:2160p", "hdencode:remux:remux"])
        assert not ok
        assert "hdencode:remux:remux" in why
        assert "parser p1" in why

    def test_with_every_required_arm_contracted_the_release_is_covered(self):
        """The other positive control: correctly-keyed contracts DO work."""
        arms = [_monotonic_arm("hdencode:4k:2160p", "movie"),
                _monotonic_arm("hdencode:remux:remux", "movie")]
        for key in ("hdencode:4k:2160p", "hdencode:remux:remux"):
            cov.ORDERING_CONTRACTS[(key, "", "p1")] = "hde/1"
        ok, _, why = CoverageEvaluator(D).covers_release(
            _report(*arms), self.TARGET,
            ["hdencode:4k:2160p", "hdencode:remux:remux"])
        assert ok, why


# --------------------------------------------------------------------------
# M18-2  duplicate_in_run is a statement about ONE arm
# --------------------------------------------------------------------------

def _two_arm_crawl(monkeypatch, remux_entries):
    """A 4K arm then a Remux arm, in that order, sharing a release."""
    sources = [_source("4K Movies", "movie", "4k"),
               _source("Remux", "movie", "remux")]
    scraper = _Scraper([
        _listing([(ONLY4K, "Only 4K 2026"), (SHARED, "Shared Film 2026")]),
        _listing(remux_entries),
    ])
    return _crawl(sources, scraper, monkeypatch)


class TestDuplicateDetectionIsPerArm:
    """`_cov_seen_canonical` was ONE set for the whole crawl. A release first
    seen in the 4K arm was therefore marked `duplicate_in_run` on its FIRST
    sighting in Remux -- and the evaluator SKIPS duplicates, so an
    out-of-order observation was removed instead of refused."""

    def test_each_arm_first_sighting_of_a_shared_release_is_eligible(
            self, monkeypatch):
        shell = _two_arm_crawl(
            monkeypatch, [(SHARED, "Shared Film 2026"), (OLDER, "Older Film 2026")])
        report = shell._last_crawl_traversal
        assert report is not None
        by_key = {a.arm_key: a for a in report.arms}
        assert len(by_key) == 2, "the two feeds collapsed into one arm"

        for key, arm in by_key.items():
            first = arm.pages[0].sightings
            shared = [s for s in first if s.canonical_url.endswith("shared-film-2026")]
            assert len(shared) == 1, "%s did not observe the shared release" % key
            assert not shared[0].duplicate_in_run, (
                "%s discarded its FIRST sighting of the shared release as a "
                "repeat, because another arm had already seen it" % key)

    def test_a_true_repeat_within_one_arm_is_still_flagged(self, monkeypatch):
        """The positive control for the flag itself. Per-arm scoping must not
        mean never flagging -- that would reopen M17-1."""
        sources = [_source("4K Movies", "movie", "4k")]
        scraper = _Scraper([_listing([
            (SHARED, "Shared Film 2026"),
            (SHARED.rstrip("/") + "/?utm_source=rss", "Shared Film 2026"),
        ])])
        shell = _crawl(sources, scraper, monkeypatch)
        sights = shell._last_crawl_traversal.arms[0].pages[0].sightings
        flags = [s.duplicate_in_run for s in sights]
        assert flags[0] is False
        assert any(flags[1:]), (
            "a cosmetic raw variant of a release already seen in THIS arm was "
            "not flagged as a repeat")

    def test_the_later_arm_refuses_an_inversion_instead_of_hiding_it(
            self, monkeypatch):
        """The shape that made this a false-proof path, not a cosmetic bug.

        Remux lists the shared release (Aug 19) ABOVE an older one, then a
        NEWER one below -- an inversion. With one global duplicate set the
        shared sighting was skipped, leaving too few anchors to notice, and the
        arm returned a quiet 'no corroborated anchor'. Scoped per arm, the
        inversion is visible and the arm refuses."""
        shell = _two_arm_crawl(monkeypatch, [
            (SHARED, "Shared Film 2026"),       # Aug 19
            (OLDER, "Older Film 2026"),         # Aug 18
            (ONLY4K, "Only 4K 2026"),           # Aug 20 -- newer, below older
        ])
        report = shell._last_crawl_traversal
        remux = [a for a in report.arms if a.arm_key.endswith("remux")][0]
        v = CoverageEvaluator(PROD_DATES).evaluate_arm(report, remux)
        assert not v.proven
        assert "inversion" in v.reason, (
            "the out-of-order observation was removed rather than refused; "
            "reason was: %s" % v.reason)


# --------------------------------------------------------------------------
# M18-3  a page speaks only for posts it actually read
# --------------------------------------------------------------------------

class TestAPageIsSealedOnlyAfterEnumeration:

    def _crawl_failing_midway(self, monkeypatch, fail_on_call):
        import backend.scanner_service as ss
        real = ss.canonicalize_listing_url
        state = {"n": 0}

        def flaky(url):
            state["n"] += 1
            if state["n"] == fail_on_call:
                raise RuntimeError("listing read failed midway")
            return real(url)

        monkeypatch.setattr(ss, "canonicalize_listing_url", flaky)
        scraper = _Scraper([_listing([
            (ONLY4K, "Only 4K 2026"),
            (SHARED, "Shared Film 2026"),
            (OLDER, "Older Film 2026"),
        ])])
        return _crawl([_source("4K Movies", "movie", "4k")], scraper, monkeypatch)

    def test_a_complete_page_is_sealed_usable(self, monkeypatch):
        """Positive control: sealing later must not mean never sealing."""
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([(ONLY4K, "Only 4K 2026"), (SHARED, "Shared 2026")])]),
            monkeypatch)
        page = shell._last_crawl_traversal.arms[0].pages[0]
        assert page.usable
        assert len(page.sightings) == 2

    def test_a_failure_midway_leaves_the_page_unusable(self, monkeypatch):
        shell = self._crawl_failing_midway(monkeypatch, fail_on_call=3)
        arms = shell._last_crawl_traversal.arms
        assert arms, "the failure erased the arm entirely"
        pages = arms[0].pages
        assert len(pages) == 1, "expected exactly one attempted page"
        page = pages[0]
        assert not page.usable, (
            "a page that raised partway through enumeration was sealed as a "
            "complete observation; the evaluator would walk a truncated list "
            "and call the shortfall a frontier")
        assert page.request_outcome == "exception"
        assert page.page_error

    def test_the_partial_sightings_are_kept_for_inspection(self, monkeypatch):
        """Unusable, but not erased. What WAS read is a true record."""
        shell = self._crawl_failing_midway(monkeypatch, fail_on_call=3)
        page = shell._last_crawl_traversal.arms[0].pages[0]
        assert len(page.sightings) >= 1
        assert len(page.sightings) < 3, "the page did not actually truncate"

    def test_the_evaluator_refuses_the_partial_page(self, monkeypatch):
        shell = self._crawl_failing_midway(monkeypatch, fail_on_call=3)
        report = shell._last_crawl_traversal
        arm = report.arms[0]
        v = CoverageEvaluator(PROD_DATES).evaluate_arm(report, arm)
        assert not v.proven
        assert "unusable" in v.reason


class TestPositionsMustBeAComplete1toN:
    """Sorting the positions concealed both a gap and a reordering."""

    def _v(self, positions):
        sights = [Sighting(position=p, canonical_url=u)
                  for p, u in zip(positions, ["u/aug20", "u/aug19", "u/aug18"])]
        arm = _arm("hdencode:4k:2160p", "movie", Page(1, sightings=sights))
        return CoverageEvaluator(D).evaluate_arm(_report(arm), arm)

    def test_a_contiguous_page_is_accepted(self):
        v = self._v([1, 2, 3])
        assert v.proven, v.reason

    def test_a_gap_refuses(self):
        """[1, 3] means a sighting was LOST between them."""
        v = self._v([1, 3])
        assert not v.proven
        assert "not a complete 1..2 sequence" in v.reason

    def test_positions_out_of_emitted_order_refuse(self):
        """[2, 1] means the emitted order and the claimed order disagree, and
        the frontier argument rests entirely on order."""
        v = self._v([2, 1])
        assert not v.proven
        assert "emitted order" in v.reason

    def test_a_page_not_starting_at_one_refuses(self):
        v = self._v([2, 3])
        assert not v.proven


# --------------------------------------------------------------------------
# M18-4  the evidence a proof cites is sealed at capture
# --------------------------------------------------------------------------

class TestEvidenceIsCapturedNotRetained:

    def test_mutating_the_caller_dict_cannot_change_a_proof(self):
        caller = dict(D)
        ev = CoverageEvaluator(caller)
        arm = _monotonic_arm("hdencode:4k:2160p", "movie")

        before = ev.evaluate_arm(_report(arm), arm)
        assert before.proven, before.reason

        caller["u/aug19"] = "January 4, 2024 at 1:00 AM"
        caller["u/aug18"] = "January 3, 2024 at 1:00 AM"

        after = ev.evaluate_arm(_report(arm), arm)
        assert after.proven
        assert after.proof.frontier_date == before.proof.frontier_date, (
            "an enrichment pass rewriting a date silently rewrote the evidence "
            "a past decision rested on")
        assert after.proof.frontier_date_raw == before.proof.frontier_date_raw

    def test_mutating_the_caller_unstable_set_cannot_change_a_proof(self):
        unstable = set()
        ev = CoverageEvaluator(dict(D), unstable)
        arm = _monotonic_arm("hdencode:4k:2160p", "movie")
        before = ev.evaluate_arm(_report(arm), arm)
        assert before.proven, before.reason

        unstable.update({"u/aug19", "u/aug18"})
        after = ev.evaluate_arm(_report(arm), arm)
        assert after.proven, (
            "the caller emptied the anchors out from under a sealed snapshot")
        assert after.proof.frontier_url == before.proof.frontier_url

    def test_the_snapshot_itself_is_read_only(self):
        snap = CoverageEvidenceSnapshot.capture(D)
        with pytest.raises(TypeError):
            snap.dates["u/aug19"] = "January 4, 2024 at 1:00 AM"
        with pytest.raises(Exception):
            snap.dates = {}

    def test_a_snapshot_can_be_passed_directly(self):
        snap = CoverageEvidenceSnapshot.capture(D)
        arm = _monotonic_arm("hdencode:4k:2160p", "movie")
        v = CoverageEvaluator(snap).evaluate_arm(_report(arm), arm)
        assert v.proven, v.reason

    def test_a_snapshot_plus_a_second_unstable_set_is_refused(self):
        """Two sources of truth for the same field. Rather than pick one,
        refuse -- the caller has expressed a contradiction."""
        snap = CoverageEvidenceSnapshot.capture(D)
        with pytest.raises(ValueError):
            CoverageEvaluator(snap, {"u/aug19"})

    def test_the_snapshot_still_reflects_the_values_at_capture(self):
        """Anti-vacuity: 'immune to mutation' must not mean 'ignores input'."""
        snap = CoverageEvidenceSnapshot.capture({"u/aug20": D["u/aug20"]})
        assert snap.dates["u/aug20"] == D["u/aug20"]
        assert "u/aug19" not in snap.dates
