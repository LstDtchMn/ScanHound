"""The crawler actually EMITS a TraversalReport, and the evaluator can use it.

The contract and the evaluator can both be perfect and still be worthless if
nothing produces the input. That has been the recurring failure in this work --
correct code that reached nobody -- so this drives the real
`ScannerService._crawl_pages()` and then feeds what it produced straight into the
real `CoverageEvaluator`.

Nothing here writes `category_attested`. The evaluator returns a proof object and
the test inspects it; no attestation exists to grant.
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from backend.scanner_service import ScannerService
from backend.coverage import CoverageEvaluator, PARSER_RECOGNISED

A = "https://hdencode.example/a-film-2026-2160p/"
B = "https://hdencode.example/b-film-2026-2160p/"
C = "https://hdencode.example/c-show-s02-2160p/"

DATES = {
    "https://hdencode.example/a-film-2026-2160p": "August 20, 2026 at 11:00 PM",
    "https://hdencode.example/b-film-2026-2160p": "August 19, 2026 at 10:00 PM",
    "https://hdencode.example/c-show-s02-2160p":  "August 18, 2026 at 9:00 PM",
}


def _listing(entries):
    rows = "".join(
        '<div class="data"><h5><a href="%s">%s</a></h5></div>' % (u, t)
        for u, t in entries)
    return ("<html><body>%s</body></html>" % rows).encode()


def _unrecognisable():
    return b"<html><body><main><p>new design</p></main></body></html>"


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self.content = body


class _Scraper:
    """Serves a queue of page bodies, in request order."""
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def get(self, *_a, **_kw):
        body = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return body if isinstance(body, _Resp) else _Resp(body)


def _shell():
    s = ScannerService.__new__(ScannerService)
    s._stop_event = threading.Event()
    s._last_crawl_seen_urls = set()
    s._last_crawl_early_stopped = False
    s._last_crawl_request_count = 0
    s._last_crawl_policy_excluded_new = []
    s._last_crawl_policy_excluded_count = 0
    s._last_crawl_page_errors = 0
    s._last_crawl_types_covered = set()
    s._log = MagicMock()
    s._progress = MagicMock()
    s.config = {}
    s.db = None
    return s


#: The REAL declared feeds, verbatim from ScannerService._build_sources.
#:
#: Round 20: identity is derived from the request definition, so a made-up base
#: like "https://hdencode.org/4k/" resolves to an 'arm.unregistered.*' id. Every
#: test built on this harness would then run against an undeclared arm and prove
#: nothing about the feeds that actually ship.
_DECLARED = {
    "4k": ("https://hdencode.org/quality/2160p/", "?tag=movies"),
    "remux": ("https://hdencode.org/quality/remux/", "?tag=movies"),
    "tv": ("https://hdencode.org/tag/tv-packs/", ""),
}


def _source(name, kind, category):
    base, suffix = _DECLARED.get(
        category, ("https://hdencode.org/%s/" % category, ""))
    return {"name": name, "base": base, "suffix": suffix, "type": kind,
            "source": "hdencode", "category": category}


def _crawl(sources, scraper, monkeypatch, pages=1):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)
    shell = _shell()

    async def run():
        loop = asyncio.get_running_loop()
        await shell._crawl_pages(
            sources, pages=pages, base_url="https://hdencode.org",
            scraper=scraper, loop=loop, previously_scanned=set(),
            early_stop=False, policy_excluded=None, skip_full_disc=False)
        return shell

    return asyncio.run(run())


class TestTheCrawlerEmitsAReport:

    def test_a_report_exists_with_arms_pages_and_ordered_sightings(self, monkeypatch):
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([(A, "A Film 2026 2160p"), (B, "B Film 2026 2160p")])]),
            monkeypatch)

        report = shell._last_crawl_traversal
        assert report is not None, "the crawl produced no traversal report at all"
        assert report.arms, "no arms recorded"
        arm = report.arms[0]
        # Round 20: the opaque declared id, not the round-19 parsed triple.
        assert arm.arm_key == "arm.hdencode.4k-2160p"
        assert arm.listing_type == "movie"
        assert arm.parser_version, "a proof needs the parser version"

        assert len(arm.pages) == 1
        page = arm.pages[0]
        assert page.parser_state == PARSER_RECOGNISED
        assert page.usable

        positions = [s.position for s in page.sightings]
        assert positions == sorted(positions), "listing order was not preserved"
        assert len(page.sightings) == 2
        assert page.sightings[0].raw_url == A, "the first listing entry moved"
        assert page.sightings[0].canonical_url.endswith("a-film-2026-2160p")

    def test_an_unparseable_page_is_recorded_as_unrecognised(self, monkeypatch):
        """Not merely 'HTTP 200'. The evaluator refuses to walk past this."""
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_unrecognisable()]), monkeypatch)
        page = shell._last_crawl_traversal.arms[0].pages[0]
        assert page.parser_state == "unrecognised"
        assert not page.usable

    def test_a_repeat_across_pages_is_flagged_not_dropped(self, monkeypatch):
        """The evaluator needs to KNOW it was there and why it cannot anchor."""
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([
                _listing([(A, "A Film 2026 2160p")]),
                _listing([(A, "A Film 2026 2160p"), (B, "B Film 2026 2160p")]),
            ]), monkeypatch, pages=2)
        arm = shell._last_crawl_traversal.arms[0]
        assert len(arm.pages) == 2
        repeats = [s for p in arm.pages for s in p.sightings if s.duplicate_in_run]
        assert repeats, "the repeated URL was not flagged as a duplicate"
        assert repeats[0].raw_url == A


class TestTheEvaluatorCanConsumeWhatTheCrawlerProduced:
    """The end-to-end join. Contract, producer and evaluator in one path."""

    def test_a_real_crawl_yields_a_usable_frontier(self, monkeypatch):
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([
                (A, "A Film 2026 2160p"),
                (B, "B Film 2026 2160p"),
                (C, "C Show S02 2160p"),
            ])]), monkeypatch)

        report = shell._last_crawl_traversal
        verdict = CoverageEvaluator(DATES).evaluate_arm(report, report.arms[0])

        assert verdict.proven, verdict.reason
        assert verdict.proof.frontier_url.endswith("b-film-2026-2160p"), (
            "the frontier should be the last CORROBORATED anchor, one short of "
            "the deepest")
        assert verdict.proof.parser_version == report.arms[0].parser_version

    def test_the_crawler_still_decides_nothing_about_coverage(self, monkeypatch):
        """S7: recording an observation is not granting a permission. The report
        carries facts and no verdict."""
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([(A, "A Film 2026 2160p")])]), monkeypatch)
        report = shell._last_crawl_traversal
        for name in ("covered", "proof", "frontier", "attested"):
            assert not hasattr(report, name), (
                "the traversal report carries a %r conclusion; deriving that is "
                "the evaluator's job, not the crawler's" % name)


class TestAnAttemptedPageIsAlwaysObserved:
    """M17-2, required change 1. The generic exception handler used to leave NO
    page record, so an exception on page 2 produced a report of pages [1, 3] and
    the absence was invisible. A page we attempted and failed is a FACT about the
    traversal; the evidence should carry it rather than rely on something
    downstream noticing a hole."""

    class _Exploding:
        """Succeeds, raises, succeeds -- the reviewer's exact sequence."""
        def __init__(self, bodies):
            self._bodies = list(bodies)
            self.calls = 0

        def get(self, *_a, **_kw):
            i = self.calls
            self.calls += 1
            if self._bodies[i] is None:
                raise RuntimeError("injected: connection reset")
            return _Resp(self._bodies[i])

    def test_the_failed_page_appears_in_the_report(self, monkeypatch):
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            self._Exploding([
                _listing([(A, "A Film 2026 2160p")]),
                None,
                _listing([(B, "B Film 2026 2160p")]),
            ]), monkeypatch, pages=3)

        arm = shell._last_crawl_traversal.arms[0]
        numbers = sorted(p.page_number for p in arm.pages)
        assert numbers == [1, 2, 3], (
            "the failed page vanished from the report, so the gap was invisible: "
            "got %s" % numbers)
        failed = [p for p in arm.pages if p.page_number == 2][0]
        assert not failed.usable
        assert failed.page_error

    def test_and_the_evaluator_refuses_that_traversal(self, monkeypatch):
        """The safety net behind the evidence: even recorded, a failed page
        before the frontier means the walk was not contiguous."""
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            self._Exploding([
                _listing([(A, "A Film 2026 2160p")]),
                None,
                _listing([(B, "B Film 2026 2160p")]),
            ]), monkeypatch, pages=3)

        report = shell._last_crawl_traversal
        v = CoverageEvaluator(DATES).evaluate_arm(report, report.arms[0])
        assert not v.proven
        assert "unusable" in v.reason or "gap" in v.reason


class TestTheCrawlerFlagsAliasesByCanonicalIdentity:
    """M17-1, at the PRODUCER.

    `TestOneCanonicalPostUnderTwoRawAliases` in the evaluator suite builds its
    Sightings by hand with duplicate_in_run=True -- so it proves the evaluator
    HONOURS the flag, and proves nothing about whether the crawler ever SETS it.
    Reverting the crawler to raw-href keying left that suite entirely green.

    This drives the real crawl with one canonical release under two cosmetic raw
    hrefs, which is the shape that let one terminal post corroborate itself.
    """

    def test_a_cosmetic_variant_is_flagged_as_a_duplicate(self, monkeypatch):
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([
                (A, "A Film 2026 2160p"),
                (A.rstrip("/") + "/?utm_source=listing", "A Film 2026 2160p"),
            ])]), monkeypatch)

        page = shell._last_crawl_traversal.arms[0].pages[0]
        assert len(page.sightings) == 2, "both raw variants must be OBSERVED"
        assert page.sightings[0].duplicate_in_run is False
        assert page.sightings[1].duplicate_in_run is True, (
            "the second raw variant of the same canonical release was not "
            "flagged, so it can corroborate the first as an independent anchor")
        assert (page.sightings[0].canonical_url
                == page.sightings[1].canonical_url), "same release, by identity"

    def test_and_the_evaluator_will_not_let_it_corroborate(self, monkeypatch):
        """End to end: the alias cannot extend the frontier."""
        old = "https://hdencode.example/old-film-2019-2160p/"
        shell = _crawl(
            [_source("4K Movies", "movie", "4k")],
            _Scraper([_listing([
                (A, "A Film 2026 2160p"),
                (B, "B Film 2026 2160p"),
                (old, "Old Film 2019 2160p"),
                (old.rstrip("/") + "/?utm=x", "Old Film 2019 2160p"),
            ])]), monkeypatch)

        dates = dict(DATES)
        dates["https://hdencode.example/old-film-2019-2160p"] = \
            "January 4, 2019 at 1:00 AM"
        report = shell._last_crawl_traversal
        v = CoverageEvaluator(dates).evaluate_arm(report, report.arms[0])
        assert v.proven, v.reason
        assert "old-film" not in v.proof.frontier_url, (
            "the aliased repeat corroborated the old terminal post, "
            "manufacturing years of coverage from one page")
