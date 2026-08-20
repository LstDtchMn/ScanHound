"""Round 13, L13-1: parser health is part of coverage, proved through the REAL crawler.

Two separate points, and the reviewer was right about both.

**The defect.** `types_covered` was recorded when a source arm was ENTERED, before
anything proved the arm returned parseable listing content. A TV arm answering
HTTP 200 with markup the selector no longer recognises would contribute "tv" to
coverage while producing zero posts. If a movie arm produced posts, the crawl-wide
result is non-empty and termination can still read "complete" -- so a movie-vs-TV
contradiction sitting in plain sight could never be observed, and the gate would
nonetheless call it "every contradicting listing type covered".

**The test gap.** My round-12 tests hand-populated `_last_crawl_types_covered` on a
fake scanner. They proved the predicate handles its input; they proved nothing
about whether the real machinery produces honest input. These drive
`ScannerService._crawl_pages()` itself, so a lie told by the producer is visible.
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from backend.scanner_service import ScannerService
from backend.background_scanner import crawl_attestation_verdict

MOVIE = "https://hdencode.example/a-film-2026-2160p/"
SHOW = "https://hdencode.example/a-show-s02-2160p/"


def _listing(entries):
    """Markup the selector recognises."""
    rows = "".join(
        '<div class="data"><h5><a href="%s">%s</a></h5></div>' % (u, t)
        for u, t in entries)
    return ("<html><body>%s</body></html>" % rows).encode()


def _unrecognisable():
    """HTTP 200, valid HTML, and nothing the post selector can find.

    This is what a layout change or an interstitial looks like from here: the
    request succeeded, the source is 'healthy', and zero posts come out."""
    return (b"<html><body><main><section class='listing-v2'>"
            b"<p>Our new design is here!</p></section></main></body></html>")


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self.content = body


class _PerSourceScraper:
    """Serves a different body depending on which arm is being fetched, so one
    arm can parse while another silently does not."""

    def __init__(self, by_marker):
        self._by_marker = by_marker
        self.calls = 0

    def get(self, url, *_a, **_kw):
        self.calls += 1
        for marker, body in self._by_marker.items():
            if marker in str(url):
                return _Resp(body)
        return _Resp(_unrecognisable())


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
    s._last_crawl_attests_coverage = True   # as a dedicated attesting crawl would
    s._log = MagicMock()
    s._progress = MagicMock()
    s.config = {}
    s.db = None
    return s


def _source(name, kind, category):
    return {"name": name, "base": "https://hdencode.org/%s/" % category,
            "suffix": "", "type": kind, "source": "hdencode",
            "category": category}


def _crawl(sources, scraper, monkeypatch):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)
    shell = _shell()

    async def run():
        loop = asyncio.get_running_loop()
        await shell._crawl_pages(
            sources, pages=1, base_url="https://hdencode.org",
            scraper=scraper, loop=loop, previously_scanned=set(),
            early_stop=False, policy_excluded=None, skip_full_disc=False)
        return shell

    return asyncio.run(run())


BOTH_ARMS = [_source("4K Movies", "movie", "4k"),
             _source("TV Packs", "tv", "tv")]


class TestAnArmEarnsCoverageOnlyByParsing:

    def test_an_unparseable_tv_arm_does_not_count_as_tv_coverage(self, monkeypatch):
        """THE FINDING. The TV arm is fetched and returns HTTP 200; the selector
        recognises nothing. Entering it must not count as having looked."""
        shell = _crawl(BOTH_ARMS, _PerSourceScraper({
            "/4k/": _listing([(MOVIE, "A Film 2026 2160p")]),
            "/tv/": _unrecognisable(),
        }), monkeypatch)
        assert "movie" in shell._last_crawl_types_covered
        assert "tv" not in shell._last_crawl_types_covered

    def test_and_the_gate_therefore_refuses_to_attest(self, monkeypatch):
        """The consumer, not just the field: a dishonest coverage set is only
        dangerous because the verdict trusts it."""
        shell = _crawl(BOTH_ARMS, _PerSourceScraper({
            "/4k/": _listing([(MOVIE, "A Film 2026 2160p")]),
            "/tv/": _unrecognisable(),
        }), monkeypatch)
        may, why = crawl_attestation_verdict(shell)
        assert may is False
        assert "tv" in why

    def test_two_parsing_arms_do_earn_coverage(self, monkeypatch):
        """POSITIVE CONTROL. Without this, simply never recording coverage would
        satisfy every assertion above while making attestation impossible."""
        shell = _crawl(BOTH_ARMS, _PerSourceScraper({
            "/4k/": _listing([(MOVIE, "A Film 2026 2160p")]),
            "/tv/": _listing([(SHOW, "A Show S02 2160p")]),
        }), monkeypatch)
        assert shell._last_crawl_types_covered >= {"movie", "tv"}
        may, why = crawl_attestation_verdict(shell)
        assert may is True, why

    def test_an_empty_but_valid_listing_also_fails_closed(self, monkeypatch):
        """Deliberate, and worth stating: an empty listing and an unparseable one
        are indistinguishable from here, so neither is allowed to prove absence."""
        shell = _crawl(BOTH_ARMS, _PerSourceScraper({
            "/4k/": _listing([(MOVIE, "A Film 2026 2160p")]),
            "/tv/": _listing([]),
        }), monkeypatch)
        assert "tv" not in shell._last_crawl_types_covered
        assert crawl_attestation_verdict(shell)[0] is False
