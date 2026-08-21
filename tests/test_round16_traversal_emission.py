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


def _source(name, kind, category):
    return {"name": name, "base": "https://hdencode.org/%s/" % category,
            "suffix": "", "type": kind, "source": "hdencode",
            "category": category}


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
        assert arm.arm_key == "hdencode:4k"
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
