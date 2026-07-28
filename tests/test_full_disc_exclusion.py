"""Full-disc ([BD]) releases are excluded by policy before any detail fetch.

Background: HDEncode full-disc rips carry no ``Filename:`` field, because a whole
disc is not a single file. ``scrape_details`` requires it and returns None
without logging, so these releases were never cached, counted as new every
cycle, and re-downloaded forever. Measured 2026-07-27: 0 of 2432 catalogued
releases were full-disc, and ~124 detail pages were re-fetched hourly for
nothing.

The early-stop tests below are the important ones. A first draft of this fix
skipped [BD] posts BEFORE they counted toward ``page_new``, which would have let
early-stop fire on a page holding new full-disc releases and hide genuinely new
releases deeper in the listing — trading one silent loss for another.
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from backend.scanner_service import ScannerService, is_full_disc_title


# ── detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "[BD]Sorority.House.Massacre.1986.2160p.GBR.UHD.Blu-ray",
    "[BD] The Mastermind 2025 2160p COMPLETE UHD BLURAY",
    "[bd] lowercase marker",
    "[ BD ] spaced brackets",
    "   [BD] leading whitespace",
])
def test_full_disc_titles_match(title):
    assert is_full_disc_title(title) is True


@pytest.mark.parametrize("title", [
    "BD Movie Title",              # no brackets
    "Some BDRip Movie 2024",       # BDRip is an ordinary encode
    "The BD Story 2024 1080p",
    "Movie [BD] Not At The Start",  # marker must be a prefix
    "",
    None,
])
def test_ordinary_titles_do_not_match(title):
    assert is_full_disc_title(title) is False


def test_url_slug_does_not_control_classification():
    """The slug for [BD]Sorority... is bdsorority..., so a substring test there
    would also catch a real release whose title merely starts with those
    letters. Classification is by title only."""
    assert is_full_disc_title("Bdelloid Rotifers 2024 1080p") is False


# ── crawl behaviour ───────────────────────────────────────────────────

def _page(entries):
    """Minimal listing markup matching the div.data h5 a selector."""
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


def _shell(config=None, db=None):
    s = ScannerService.__new__(ScannerService)
    s._stop_event = threading.Event()
    s._last_crawl_seen_urls = set()
    s._last_crawl_early_stopped = False
    s._last_crawl_request_count = 0
    s._last_crawl_policy_excluded_new = []
    s._last_crawl_policy_excluded_count = 0
    s._log = MagicMock()
    s._progress = MagicMock()
    s.config = config if config is not None else {}
    s.db = db
    return s


_SOURCE = {
    "name": "4K Movies",
    "base": "https://hdencode.org/quality/2160p/",
    "suffix": "?tag=movies",
    "type": "movie",
    "source": "hdencode",
    "category": "4k",
}


def _crawl(scanner, scraper, monkeypatch, *, pages=1, previously_scanned=None,
           early_stop=False, policy_excluded=None, skip_full_disc=None,
           source=None):
    async def no_sleep(_s):
        return None
    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", no_sleep)

    async def run():
        loop = asyncio.get_running_loop()
        return await scanner._crawl_pages(
            [source or _SOURCE], pages=pages,
            base_url="https://hdencode.org", scraper=scraper, loop=loop,
            previously_scanned=previously_scanned or set(),
            early_stop=early_stop,
            policy_excluded=policy_excluded,
            skip_full_disc=skip_full_disc,
        )
    return asyncio.run(run())


def test_full_disc_release_is_never_scheduled_for_detail(monkeypatch):
    scraper = _Scraper([_page([
        ("https://hdencode.org/ordinary-2024-2160p/", "Ordinary 2024 2160p"),
        ("https://hdencode.org/bdfull-disc/", "[BD]Full.Disc.2024.COMPLETE"),
    ])])
    posts = _crawl(_shell(), scraper, monkeypatch, skip_full_disc=True)

    urls = [p["url"] for p in posts]
    assert urls == ["https://hdencode.org/ordinary-2024-2160p/"]
    assert not any("bdfull-disc" in u for u in urls)


def test_ordinary_releases_are_unaffected(monkeypatch):
    entries = [("https://hdencode.org/m%d/" % i, "Movie %d 2024 2160p" % i)
               for i in range(5)]
    scraper = _Scraper([_page(entries)])
    posts = _crawl(_shell(), scraper, monkeypatch, skip_full_disc=True)
    assert len(posts) == 5


def test_disabling_the_policy_schedules_legacy_processing_not_ingestion(monkeypatch):
    """Disabling the policy does NOT ingest full discs.

    detail_scraper still requires a Filename field these pages do not have, so
    the flag only routes them back down the old silent-failure path. An earlier
    version of this test asserted the URL was returned and called that proof the
    escape hatch "works" — it proved scheduling, not ingestion.
    """
    scanner = _shell()
    scraper = _Scraper([_page([
        ("https://hdencode.org/bdfull-disc/", "[BD]Full.Disc.2024"),
    ])])
    posts = _crawl(scanner, scraper, monkeypatch, skip_full_disc=False)

    assert len(posts) == 1, "scheduled for the legacy path"
    warnings = [c for c in scanner._log.call_args_list
                if len(c.args) > 1 and c.args[1] == "warning"]
    assert warnings, "disabling an unsupported policy must warn"
    assert "still fail to parse" in warnings[0].args[0]


def test_non_hdencode_sources_are_unaffected(monkeypatch):
    other = dict(_SOURCE, source="ddlbase")
    scraper = _Scraper([_page([
        ("https://ddlbase.com/post/bd-thing/", "[BD]Something 2024"),
    ])])
    posts = _crawl(_shell(), scraper, monkeypatch, skip_full_disc=True,
                   source=other)
    assert len(posts) == 1, "policy is HDEncode-specific"


def test_new_exclusions_are_exposed_for_persistence(monkeypatch):
    scraper = _Scraper([_page([
        ("https://hdencode.org/bda/", "[BD]A 2024"),
        ("https://hdencode.org/bdb/", "[BD]B 2024"),
    ])])
    scanner = _shell()
    _crawl(scanner, scraper, monkeypatch, skip_full_disc=True)

    rows = scanner._last_crawl_policy_excluded_new
    # stored under the canonical identity, with the raw href kept alongside
    assert {r["url"] for r in rows} == {
        "https://hdencode.org/bda", "https://hdencode.org/bdb"}
    assert {r["raw_url"] for r in rows} == {
        "https://hdencode.org/bda/", "https://hdencode.org/bdb/"}
    assert all(r["title"].startswith("[BD]") for r in rows)
    assert scanner._last_crawl_policy_excluded_count == 2


def test_known_exclusion_is_not_re_reported(monkeypatch):
    scraper = _Scraper([_page([
        ("https://hdencode.org/bda/", "[BD]A 2024"),
    ])])
    scanner = _shell()
    _crawl(scanner, scraper, monkeypatch, skip_full_disc=True,
           policy_excluded={"https://hdencode.org/bda/"})

    assert scanner._last_crawl_policy_excluded_new == []
    assert scanner._last_crawl_policy_excluded_count == 1


# ── early-stop: the case the first draft got wrong ────────────────────

def test_newly_seen_exclusion_does_not_trigger_early_stop(monkeypatch):
    """Page 1 = all cached + one NEW full-disc release. Early-stop must NOT
    fire, or genuinely new releases on page 2 are never reached."""
    cached = "https://hdencode.org/cached/"
    page1 = _page([
        (cached, "Cached Movie 2024 2160p"),
        ("https://hdencode.org/bdnew/", "[BD]Brand.New.Disc.2024"),
    ])
    page2 = _page([
        ("https://hdencode.org/genuinely-new/", "Genuinely New 2024 2160p"),
    ])
    scanner = _shell()
    posts = _crawl(scanner, _Scraper([page1, page2]), monkeypatch,
                   pages=2, previously_scanned={cached},
                   early_stop=True, skip_full_disc=True)

    assert scanner._last_crawl_early_stopped is False
    assert "https://hdencode.org/genuinely-new/" in [p["url"] for p in posts], (
        "a new full-disc release hid eligible content deeper in the listing"
    )


def test_known_exclusion_allows_early_stop(monkeypatch):
    """Page 1 = all cached + an ALREADY-KNOWN full-disc release. Nothing new
    here, so early-stop is correct and the crawl should not go deeper."""
    cached = "https://hdencode.org/cached/"
    known_bd = "https://hdencode.org/bdknown/"
    page1 = _page([
        (cached, "Cached Movie 2024 2160p"),
        (known_bd, "[BD]Known.Disc.2024"),
    ])
    page2 = _page([
        ("https://hdencode.org/deeper/", "Deeper 2024 2160p"),
    ])
    scanner = _shell()
    scraper = _Scraper([page1, page2])
    _crawl(scanner, scraper, monkeypatch, pages=2,
           previously_scanned={cached}, early_stop=True,
           policy_excluded={known_bd}, skip_full_disc=True)

    assert scanner._last_crawl_early_stopped is True
    assert scraper.calls == 1, "should not have fetched page 2"


def test_exclusions_do_not_force_deep_crawling_forever(monkeypatch):
    """Second cycle over the same listing: the exclusion is known by then, so
    the crawl settles instead of re-crawling deeply every hour."""
    cached = "https://hdencode.org/cached/"
    bd = "https://hdencode.org/bdx/"
    page = _page([(cached, "Cached 2024"), (bd, "[BD]X 2024")])

    first = _shell()
    scraper1 = _Scraper([page, page])
    _crawl(first, scraper1, monkeypatch, pages=2,
           previously_scanned={cached}, early_stop=True, skip_full_disc=True)
    # page 1 held a NEW exclusion, so the crawl was allowed past it. (It then
    # stops on page 2, which is correct - page 2 genuinely had nothing new.)
    assert scraper1.calls == 2
    learned = {r["url"] for r in first._last_crawl_policy_excluded_new}

    second = _shell()
    scraper2 = _Scraper([page, page])
    _crawl(second, scraper2, monkeypatch, pages=2,
           previously_scanned={cached}, early_stop=True,
           policy_excluded=learned, skip_full_disc=True)

    assert second._last_crawl_early_stopped is True
    assert scraper2.calls == 1


# ── canonical identity ────────────────────────────────────────────────

from backend.scanner_service import canonicalize_listing_url


@pytest.mark.parametrize("a,b", [
    ("https://hdencode.org/bda/", "https://hdencode.org/bda"),
    ("https://hdencode.org/bda/", "https://hdencode.org/bda/?utm=x"),
    ("https://hdencode.org/bda/", "https://hdencode.org/bda/#frag"),
    ("https://hdencode.org/bda/", "https://HDEncode.org/bda/"),
    ("https://hdencode.org/bda/", "HTTPS://hdencode.org/bda/"),
])
def test_url_variants_collapse_to_one_identity(a, b):
    assert canonicalize_listing_url(a) == canonicalize_listing_url(b)


def test_distinct_posts_stay_distinct():
    assert (canonicalize_listing_url("https://hdencode.org/bda/")
            != canonicalize_listing_url("https://hdencode.org/bdb/"))


# ── the no-silent-skip contract ───────────────────────────────────────

def test_exclusions_produce_exactly_one_aggregate_info_line(monkeypatch):
    """A mutation deleting this log would otherwise survive the whole suite,
    recreating the observability failure this work exists to end."""
    entries = [("https://hdencode.org/bd%d/" % i, "[BD]Disc %d" % i)
               for i in range(20)]
    scanner = _shell()
    _crawl(scanner, _Scraper([_page(entries)]), monkeypatch, skip_full_disc=True)

    lines = [c.args[0] for c in scanner._log.call_args_list
             if "full-disc" in str(c.args[0])]
    assert len(lines) == 1, "aggregate once per crawl, never per release"
    assert "20" in lines[0]
    assert "newly seen" in lines[0]
    assert "before detail fetch" in lines[0]


def test_zero_exclusions_emits_no_policy_line(monkeypatch):
    scanner = _shell()
    _crawl(scanner, _Scraper([_page([
        ("https://hdencode.org/ordinary/", "Ordinary 2024 2160p"),
    ])]), monkeypatch, skip_full_disc=True)
    assert not [c for c in scanner._log.call_args_list
                if "full-disc" in str(c.args[0])]


def test_known_only_crawl_still_reports_the_count(monkeypatch):
    scanner = _shell()
    _crawl(scanner, _Scraper([_page([
        ("https://hdencode.org/bda/", "[BD]A"),
    ])]), monkeypatch, skip_full_disc=True,
        policy_excluded={canonicalize_listing_url("https://hdencode.org/bda/")})

    lines = [c.args[0] for c in scanner._log.call_args_list
             if "full-disc" in str(c.args[0])]
    assert len(lines) == 1
    assert "0 newly seen" in lines[0]


# ── exclusion-only early-stop (no ordinary cached baseline) ───────────

def test_known_exclusions_alone_can_settle_the_crawl(monkeypatch):
    """The persisted exclusion store IS prior observation.

    Requiring a non-empty ordinary skip_urls meant a listing whose only known
    content was excluded releases never settled, and every configured page was
    re-crawled every cycle — the waste this work exists to remove.
    """
    bd = "https://hdencode.org/bdonly/"
    page1 = _page([(bd, "[BD]Only.Known.Disc")])
    page2 = _page([("https://hdencode.org/deeper/", "Deeper 2024")])

    scanner = _shell()
    scraper = _Scraper([page1, page2])
    _crawl(scanner, scraper, monkeypatch, pages=2,
           previously_scanned=set(),                 # no ordinary cache at all
           early_stop=True,
           policy_excluded={canonicalize_listing_url(bd)},
           skip_full_disc=True)

    assert scanner._last_crawl_early_stopped is True
    assert scraper.calls == 1, "re-crawled despite having no new content"


# ── durable round-trip through a real database ────────────────────────

def test_exclusion_survives_and_last_seen_advances(db_manager):
    """first_seen_at is preserved, last_seen_at advances, one row only."""
    url = "https://hdencode.org/bdpersist/"
    row = {"url": url, "title": "[BD]Persist", "source": "hdencode",
           "category": "4k"}

    db_manager.record_policy_exclusions([row])
    assert db_manager.get_policy_excluded_urls("hdencode") == {url}

    # age the row deterministically rather than relying on wall-clock drift
    conn = db_manager.get_connection()
    conn.execute(
        "UPDATE listing_policy_exclusions SET first_seen_at=?, last_seen_at=?",
        ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
    conn.commit()

    db_manager.record_policy_exclusions([row])

    cur = conn.execute(
        "SELECT canonical_url, first_seen_at, last_seen_at, source, "
        "policy_reason, COUNT(*) AS n FROM listing_policy_exclusions")
    got = cur.fetchone()
    assert got["n"] == 1, "re-observation must upsert, not duplicate"
    assert got["first_seen_at"] == "2020-01-01T00:00:00+00:00"
    assert got["last_seen_at"] > got["first_seen_at"], "last_seen_at froze"
    assert got["policy_reason"] == "listing_policy_excluded_full_disc"


def test_every_observed_exclusion_is_persisted_not_just_new(monkeypatch):
    """Known exclusions must reach the persistence payload too, or last_seen_at
    can never advance for anything already in the store."""
    bd = "https://hdencode.org/bdknown2/"
    scanner = _shell()
    _crawl(scanner, _Scraper([_page([(bd, "[BD]Known")])]), monkeypatch,
           skip_full_disc=True,
           policy_excluded={canonicalize_listing_url(bd)})

    assert scanner._last_crawl_policy_excluded_new == []
    observed = scanner._last_crawl_policy_excluded_observed
    assert len(observed) == 1
    assert observed[0]["url"] == canonicalize_listing_url(bd)
