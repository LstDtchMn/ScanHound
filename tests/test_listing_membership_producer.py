"""Tests for the per-crawl listing-membership producer in scanner_service.py.

Background: a coverage canary needs to know which URLs each listing source
showed, per cycle, with how deep they were. The crawler keeps ONE
``seen_post_urls`` set for the whole crawl and short-circuits on it inside
``_crawl_pages`` -- so a release listed on both the 4K page and the Remux
page is processed once and, after that short-circuit, is invisible for the
second source. Recording membership must happen BEFORE the short-circuit,
with the source key taken from the source CURRENTLY being traversed, or
source-scoped evidence becomes first-source-wins -- exactly what the
systematic-gap check must not see.

This file drives ``_crawl_pages()`` directly, the same way
``tests/test_full_disc_exclusion.py`` and
``tests/test_scan_block_cancellation.py`` do: a ``__new__``-built
ScannerService shell and a fake scraper, no network, no database.
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from backend.scanner_service import ScannerService
from backend.hdencode_shadow import canonical_url as hdencode_shadow_canonical_url
from backend.url_identity import canonicalize_listing_url


# ── fixtures / helpers (style matches tests/test_full_disc_exclusion.py) ───

def _page(entries):
    """Minimal listing markup matching the ``div.data h5 a`` selector."""
    rows = "".join(
        '<div class="data"><h5><a href="%s">%s</a></h5></div>' % (url, title)
        for url, title in entries
    )
    return ("<html><body>%s</body></html>" % rows).encode()


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self.content = body


class _Scraper:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def get(self, *_a, **_kw):
        body = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return _Resp(body)


def _shell():
    s = ScannerService.__new__(ScannerService)
    s._stop_event = threading.Event()
    s._last_crawl_seen_urls = set()
    s._last_crawl_early_stopped = False
    s._last_crawl_request_count = 0
    s._last_crawl_policy_excluded_new = []
    s._last_crawl_policy_excluded_count = 0
    # __new__ skips __init__, so the accumulator this file exercises must be
    # set here explicitly, exactly like every other _last_crawl_* attribute
    # the existing shells already set.
    s._last_crawl_membership = []
    s._log = MagicMock()
    s._progress = MagicMock()
    s.config = {}
    s.db = None
    return s


SOURCE_4K = {
    "name": "4K Movies",
    "base": "https://hdencode.org/quality/2160p/",
    "suffix": "?tag=movies",
    "type": "movie",
    "source": "hdencode",
    "category": "4k",
}

SOURCE_REMUX = dict(
    SOURCE_4K, name="Remux", base="https://hdencode.org/quality/remux/",
    category="remux",
)


def _crawl(scanner, scraper, monkeypatch, *, sources, pages=1,
           previously_scanned=None, early_stop=False):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)

    async def run():
        loop = asyncio.get_running_loop()
        return await scanner._crawl_pages(
            sources, pages=pages,
            base_url="https://hdencode.org", scraper=scraper, loop=loop,
            previously_scanned=previously_scanned or set(),
            early_stop=early_stop,
        )
    return asyncio.run(run())


# ── 1. one URL, two sources: two membership entries, one processed post ───

def test_url_on_two_sources_produces_two_membership_entries(monkeypatch):
    """The defining case: a release on both the 4K and Remux listings.

    Write this so that moving the membership append to AFTER the
    seen_post_urls short-circuit makes it fail -- see the report for the
    experiment that confirms it.
    """
    shared = "https://hdencode.org/shared-release-2024-2160p/"
    scraper = _Scraper([_page([(shared, "Shared Release 2024 2160p")])])
    scanner = _shell()

    posts = _crawl(scanner, scraper, monkeypatch, sources=[SOURCE_4K, SOURCE_REMUX])

    # Ordinary discovery still dedups the shared URL to ONE processed post --
    # the crawl's dedup behaviour must be untouched by this change.
    assert [p["url"] for p in posts] == [shared]

    canonical = hdencode_shadow_canonical_url(shared)
    entries = [m for m in scanner._last_crawl_membership if m["canonical_url"] == canonical]
    assert len(entries) == 2, (
        f"expected one membership entry per source for a shared URL, got {entries}")
    assert {e["source_key"] for e in entries} == {"hdencode:4k", "hdencode:remux"}


def test_url_on_one_source_only_produces_one_entry(monkeypatch):
    solo = "https://hdencode.org/solo-release-2024-2160p/"
    scraper = _Scraper([_page([(solo, "Solo Release 2024 2160p")])])
    scanner = _shell()

    _crawl(scanner, scraper, monkeypatch, sources=[SOURCE_4K])

    canonical = hdencode_shadow_canonical_url(solo)
    entries = [m for m in scanner._last_crawl_membership if m["canonical_url"] == canonical]
    assert len(entries) == 1
    assert entries[0]["source_key"] == "hdencode:4k"


# ── 2. one source, two pages: two entries, one per page, not collapsed ────

def test_same_url_on_two_pages_of_one_source_produces_two_entries(monkeypatch):
    url = "https://hdencode.org/repeat-2024-2160p/"
    page1 = _page([(url, "Repeat 2024 2160p")])
    page2 = _page([(url, "Repeat 2024 2160p")])
    scanner = _shell()
    scraper = _Scraper([page1, page2])

    _crawl(scanner, scraper, monkeypatch, sources=[SOURCE_4K], pages=2)

    canonical = hdencode_shadow_canonical_url(url)
    entries = [m for m in scanner._last_crawl_membership if m["canonical_url"] == canonical]
    assert len(entries) == 2, "the producer collapsed a same-source repeat across pages"
    assert sorted(e["page_index"] for e in entries) == [1, 2]
    assert all(e["source_key"] == "hdencode:4k" for e in entries)


# ── 3. rank_on_page position, page_index is 1-based ────────────────────────

def test_rank_on_page_reflects_position_and_page_index_is_one_based(monkeypatch):
    ordered = [
        ("https://hdencode.org/first-2024-2160p/", "First 2024 2160p"),
        ("https://hdencode.org/second-2024-2160p/", "Second 2024 2160p"),
        ("https://hdencode.org/third-2024-2160p/", "Third 2024 2160p"),
    ]
    scanner = _shell()
    scraper = _Scraper([_page(ordered)])

    _crawl(scanner, scraper, monkeypatch, sources=[SOURCE_4K], pages=1)

    by_canonical = {m["canonical_url"]: m for m in scanner._last_crawl_membership}
    assert len(by_canonical) == 3
    for expected_rank, (url, _title) in enumerate(ordered):
        entry = by_canonical[hdencode_shadow_canonical_url(url)]
        assert entry["rank_on_page"] == expected_rank
        assert entry["page_index"] == 1


# ── 4. empty at the start of a fresh crawl; no leakage between runs ────────

def test_membership_accumulator_is_reset_at_run_scan_entry(monkeypatch):
    """The accumulator is reset in run_scan(), at the exact place
    _last_crawl_seen_urls is reset, so nothing survives from a previous run.

    Drives the REAL run_scan() entry/reset logic with _run_scan_async
    replaced by a no-op, so this proves the reset statement itself runs
    without needing the network/Plex/DB machinery the full crawl requires.
    """
    scanner = ScannerService.__new__(ScannerService)
    scanner._stop_event = threading.Event()
    scanner._scanning_lock = threading.Lock()
    scanner._items_lock = threading.Lock()
    scanner.items = []
    scanner._item_counter = 0
    # Simulate leftovers from a previous run that a caller never reset.
    scanner._last_crawl_seen_urls = {"https://stale/"}
    scanner._last_crawl_membership = [{
        "source_key": "stale:leftover", "canonical_url": "https://stale",
        "page_index": 1, "rank_on_page": 0,
    }]
    scanner._last_crawl_request_count = 7
    scanner._last_crawl_termination = "complete"
    scanner._last_crawl_status = "complete"
    scanner._last_crawl_page_errors = 3
    scanner._last_crawl_detail_scheduled = {"stale"}
    scanner._last_crawl_detail_completed = {"stale"}
    scanner._load_download_history = lambda: set()
    scanner.matching = MagicMock()
    scanner._log = MagicMock()
    scanner._progress = MagicMock()

    async def _noop_run_scan_async(*_a, **_kw):
        return None
    scanner._run_scan_async = _noop_run_scan_async

    scanner.run_scan("Incremental", "HDEncode", pages=1)

    assert scanner._last_crawl_membership == [], (
        "a previous run's membership entries leaked into this run")


def test_fresh_shell_starts_with_an_empty_accumulator(monkeypatch):
    """A crawl that finds nothing must not manufacture membership either."""
    scanner = _shell()
    scraper = _Scraper([_page([])])

    _crawl(scanner, scraper, monkeypatch, sources=[SOURCE_4K])

    assert scanner._last_crawl_membership == []


# ── 5. canonical_url is backend.hdencode_shadow.canonical_url's output ────

def test_canonical_url_matches_hdencode_shadow_for_trailing_slash_and_query(monkeypatch):
    trailing = "https://hdencode.org/trailing-slash-2024-2160p/"
    query = "https://hdencode.org/query-string-2024-2160p/?utm=abc"
    scanner = _shell()
    scraper = _Scraper([_page([
        (trailing, "Trailing 2024 2160p"),
        (query, "Query 2024 2160p"),
    ])])

    _crawl(scanner, scraper, monkeypatch, sources=[SOURCE_4K], pages=1)

    by_rank = {m["rank_on_page"]: m["canonical_url"] for m in scanner._last_crawl_membership}
    assert by_rank[0] == hdencode_shadow_canonical_url(trailing)
    assert by_rank[1] == hdencode_shadow_canonical_url(query)
    # The query string must actually have been stripped by that function, not
    # merely passed through unmodified.
    assert "utm" not in by_rank[1]
    assert by_rank[1].endswith("query-string-2024-2160p")


# ── 6. guard: the two canonicalizers must not diverge on absolute URLs ────

@pytest.mark.parametrize("url", [
    "https://hdencode.org/plain-2024-2160p/",
    "https://hdencode.org/plain-2024-2160p",
    "https://hdencode.org/with-query-2024/?utm=x",
    "https://hdencode.org/with-frag-2024/#section",
    "HTTPS://HDEncode.ORG/Case-Preserved-Path/",
    "https://hdencode.org/",
])
def test_hdencode_shadow_and_url_identity_agree_on_absolute_urls(url):
    """A guard, not a feature test.

    Membership is compared against the feed_only/duplicate_urls sets that
    hdencode_shadow builds with its OWN canonical_url. If that function and
    url_identity.canonicalize_listing_url (used elsewhere in this same crawl,
    for the policy-exclusion store) ever diverge for an ordinary absolute
    http(s) URL, membership evidence would silently split identity from the
    comparison it feeds. Catch that divergence here, not as a fabricated
    coverage gap downstream.
    """
    assert hdencode_shadow_canonical_url(url) == canonicalize_listing_url(url)
