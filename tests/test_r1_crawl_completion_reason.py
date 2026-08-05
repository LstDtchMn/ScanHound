"""R-1 — a partially broken listing crawl must not be RSS promotion evidence.

Peer-review finding (HIGH): ``_crawl_pages`` collapsed a healthy stop at the
cached-content frontier, a Cloudflare block, a coordinator refusal, a transport
failure and a cancellation into one boolean, ``_last_crawl_early_stopped``.
``BackgroundScanner._listing_arm_incomplete`` therefore could not use it, and
fell back to "was the seen-set empty?" — which page 1 alone makes non-empty. So
the cycle "page 1 parsed, page 2 blocked" was recorded as
``outcome='success', normal_feeds_complete=1`` and counted toward the evidence
for switching HDEncode discovery from listing crawls to RSS. A wrong answer
there corrupts a decision record, not just a log line.

Two layers, because the crawl-level flag is only interesting if the consumer
acts on it (the reason field is produced in scanner_service and consumed in
background_scanner + the readiness SQL):

* PRODUCER — the real ``ScannerService._crawl_pages`` against a fake transport,
  asserting the reason it publishes.
* CONSUMER — the real ``BackgroundScanner.scan_once`` driving that same real
  ``_crawl_pages``, a real ``DatabaseManager``, and the real readiness query,
  asserting whether the cycle counts as promotion evidence.

POSITIVE CONTROLS are marked. A "fix" that marks every cycle incomplete stalls
the promotion gate forever, which is exactly as bad as counting broken cycles,
so those controls have to fail such a fix.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest

from backend.background_scanner import BackgroundScanner
from backend.database import DatabaseManager
from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficDenied,
)
from backend.scanner_service import ScannerService


# ── fake transport / coordinator ──────────────────────────────────────

def _listing_html(*urls):
    """A listing page in the shape ``_select_posts`` expects for hdencode.

    Titles deliberately avoid the ``[BD]`` prefix ``is_full_disc_title`` matches,
    so the policy-exclusion branch stays out of these tests.
    """
    items = "".join(
        f'<div class="data"><h5><a href="{url}">'
        f'Some Movie {index} 2024 2160p WEB-DL DDP5.1 x265-GRP</a></h5></div>'
        for index, url in enumerate(urls, start=1)
    )
    return f"<html><body>{items}</body></html>".encode()


class _Response:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class _Scraper:
    """Returns one scripted outcome per page fetch.

    An outcome is an int (HTTP status with an empty body), a ``_Response``, or an
    exception INSTANCE — raised to stand in for a connection reset/timeout, which
    is the case that used to leave no trace at all on the crawl flags.
    """

    def __init__(self, outcomes, *, on_get=None):
        self._outcomes = list(outcomes)
        self._on_get = on_get
        self.calls = 0

    def get(self, *_args, **_kwargs):
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if self._on_get is not None:
            self._on_get(self.calls)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, _Response):
            return outcome
        return _Response(outcome)


class _FakeCoordinator:
    """Stands in for the process-wide HDEncode traffic coordinator.

    ``observe_http_status`` returns None on purpose: a None decision keeps
    ``_crawl_pages`` out of its "confirmed shared block" branch, so these tests
    exercise the completion-reason logic rather than the coordinator's own
    block-confirmation heuristics (which have their own suite in
    tests/test_scan_block_cancellation.py).
    """

    def __init__(self, denial=None):
        self._denial = denial

    @contextlib.contextmanager
    def request(self, _request_class, **_kwargs):
        if self._denial is not None:
            raise self._denial
        yield

    def observe_http_status(self, _status):
        return None


def _install(monkeypatch, coordinator=None):
    monkeypatch.setattr(
        "backend.scanner_service.get_hdencode_coordinator",
        lambda: coordinator or _FakeCoordinator(),
    )

    async def _no_sleep(_seconds):
        return None

    # The blocked path backs off with asyncio.sleep(0.5 * streak); real sleeps
    # would make this suite seconds slower and prove nothing.
    monkeypatch.setattr("backend.scanner_service.asyncio.sleep", _no_sleep)


def _source(name="4K Movies"):
    return {
        "name": name,
        "base": "https://hdencode.org/quality/2160p/",
        "suffix": "?tag=movies",
        "type": "movie",
        "source": "hdencode",
        "category": "4k",
    }


def _scanner_shell():
    """A real ScannerService with only what ``_crawl_pages`` touches.

    __new__ rather than __init__ so the test needs no Plex/scrapers/DB/config —
    the point is that the REAL crawl code runs and publishes the real flags.
    ``_last_crawl_completion_reason`` is seeded to the same pessimistic value
    ``__init__``/``run_scan`` use, so a crawl that silently forgets to publish a
    reason cannot be mistaken for a complete one.
    """
    scanner = ScannerService.__new__(ScannerService)
    scanner._stop_event = threading.Event()
    scanner._scan_slot = threading.Lock()
    scanner._last_crawl_seen_urls = set()
    scanner._last_crawl_early_stopped = True
    scanner._last_crawl_request_count = 0
    scanner._last_crawl_completion_reason = "not_started"
    scanner.logged = []
    scanner._log = lambda msg, level="info": scanner.logged.append((level, msg))
    scanner._progress = lambda *_args, **_kwargs: None
    return scanner


def _crawl(scanner, sources, *, pages, skip_urls=None, early_stop=False):
    async def _run():
        loop = asyncio.get_running_loop()
        return await scanner._crawl_pages(
            sources,
            pages,
            "https://hdencode.org",
            scanner.scraper,
            loop,
            set(skip_urls or ()),
            early_stop,
        )

    return asyncio.run(_run())


# ── PRODUCER: the reason _crawl_pages publishes ───────────────────────

class TestCrawlPublishesWhyItStopped:
    def test_page_one_parsed_and_page_two_blocked_reports_blocked(
            self, monkeypatch):
        """THE FINDING. Page 1 fills the seen-set, page 2 is Cloudflare-blocked.

        The seen-set is non-empty and no exception escapes, so every signal that
        existed before this fix says the crawl was fine.
        """
        _install(monkeypatch)
        scanner = _scanner_shell()
        scanner.scraper = _Scraper([
            _Response(200, _listing_html("https://hdencode.org/p1/")),
            403,
        ])

        posts = _crawl(scanner, [_source()], pages=2)

        assert [p["url"] for p in posts] == ["https://hdencode.org/p1/"]
        # Both pre-existing signals still look healthy — this is why the reason
        # field had to be added rather than the old ones reinterpreted.
        assert scanner._last_crawl_seen_urls == {"https://hdencode.org/p1/"}
        assert scanner._last_crawl_completion_reason == "blocked"
        assert scanner._last_crawl_early_stopped is True

    def test_every_page_fetched_reports_complete(self, monkeypatch):
        """POSITIVE CONTROL for the producer. Nothing adverse happened, so the
        crawl must say so — a fix that never reports ``complete`` disables both
        the background purge and the promotion gate."""
        _install(monkeypatch)
        scanner = _scanner_shell()
        scanner.scraper = _Scraper([
            _Response(200, _listing_html("https://hdencode.org/p1/")),
            _Response(200, _listing_html("https://hdencode.org/p2/")),
        ])

        posts = _crawl(scanner, [_source()], pages=2)

        assert len(posts) == 2
        assert scanner._last_crawl_completion_reason == "complete"
        assert scanner._last_crawl_early_stopped is False

    def test_stop_at_the_cached_frontier_reports_cached_frontier(
            self, monkeypatch):
        """DISAGREEING CASE for "any early stop is a broken crawl".

        The background pre-cache always runs with early_stop=True, so hitting
        already-cached content is the NORMAL healthy outcome. An implementation
        that maps early_stopped to a single ineligible reason passes the blocked
        test above and stalls the shadow evidence forever.
        """
        _install(monkeypatch)
        scanner = _scanner_shell()
        known = "https://hdencode.org/already-known/"
        scanner.scraper = _Scraper([
            _Response(200, _listing_html(known)),
            _Response(200, _listing_html("https://hdencode.org/deeper/")),
        ])

        posts = _crawl(scanner, [_source()], pages=2,
                       skip_urls={known}, early_stop=True)

        assert posts == []                      # page 1 was entirely known
        assert scanner.scraper.calls == 1       # page 2 was never fetched
        assert scanner._last_crawl_seen_urls == {known}
        assert scanner._last_crawl_completion_reason == "cached_frontier"
        assert scanner._last_crawl_early_stopped is True

    def test_page_that_raised_reports_transport_error(self, monkeypatch):
        """A thrown fetch is swallowed and the loop moves on, so before this fix
        a connection reset on page 2 left early_stopped False AND the crawl
        looking complete — the worst of the set, because the caller then also
        purged the cache against a partial seen-set."""
        _install(monkeypatch)
        scanner = _scanner_shell()
        scanner.scraper = _Scraper([
            _Response(200, _listing_html("https://hdencode.org/p1/")),
            ConnectionResetError("peer reset the connection"),
            _Response(200, _listing_html("https://hdencode.org/p3/")),
        ])

        posts = _crawl(scanner, [_source()], pages=3)

        # Page 3 still crawled: one bad page must not abandon the source.
        assert [p["url"] for p in posts] == [
            "https://hdencode.org/p1/", "https://hdencode.org/p3/"]
        assert scanner._last_crawl_completion_reason == "transport_error"
        assert scanner._last_crawl_early_stopped is True

    def test_coordinator_refusal_reports_coordinator_stopped(self, monkeypatch):
        _install(monkeypatch, _FakeCoordinator(
            denial=HDEncodeTrafficDenied("cooldown", "HDEncode traffic is paused")))
        scanner = _scanner_shell()
        scanner.scraper = _Scraper([200])

        posts = _crawl(scanner, [_source()], pages=2)

        assert posts == []
        assert scanner.scraper.calls == 0        # refused before transport
        assert scanner._last_crawl_completion_reason == "coordinator_stopped"

    def test_cancelled_request_is_not_reported_as_a_coordinator_stop(
            self, monkeypatch):
        """DISAGREEING CASE for "catch both coordinator exceptions, call it
        coordinator_stopped". HDEncodeRequestCancelled is a SUBCLASS of
        HDEncodeTrafficDenied, so a single except clause catches both and the
        isinstance order is what keeps them apart. Cancellation means the scan
        was already being stopped (shutdown, /scan/stop) — a different fact from
        the coordinator refusing traffic, and the one an operator reading a
        rejected cycle needs to see.
        """
        _install(monkeypatch, _FakeCoordinator(denial=HDEncodeRequestCancelled()))
        scanner = _scanner_shell()
        scanner.scraper = _Scraper([200])

        _crawl(scanner, [_source()], pages=2)

        assert scanner._last_crawl_completion_reason == "cancelled"

    def test_operator_stop_midway_reports_cancelled(self, monkeypatch):
        """A stop set while the crawl is running (the /scan/stop route, app
        shutdown, BackgroundScanner.stop) breaks out of the page loop. That path
        set no flag at all, so the crawl reported itself complete with a
        one-page seen-set."""
        _install(monkeypatch)
        scanner = _scanner_shell()

        def _stop_after_first(call_count):
            if call_count == 1:
                scanner.stop_scan_flag = True

        scanner.scraper = _Scraper(
            [_Response(200, _listing_html("https://hdencode.org/p1/"))],
            on_get=_stop_after_first,
        )

        _crawl(scanner, [_source()], pages=3)

        assert scanner.scraper.calls == 1
        assert scanner._last_crawl_completion_reason == "cancelled"
        assert scanner._last_crawl_early_stopped is True

    def test_a_block_on_one_source_outranks_a_frontier_stop_on_another(
            self, monkeypatch):
        """DISAGREEING CASE for "assign the reason, last write wins".

        A crawl spans several sources (4K / Remux / TV). If source A is blocked
        and source B then stops cleanly at the frontier, a last-write-wins
        implementation publishes ``cached_frontier`` — an eligible reason — and
        the broken source vanishes again. The worst reason must win.
        """
        _install(monkeypatch)
        scanner = _scanner_shell()
        known = "https://hdencode.org/already-known/"
        scanner.scraper = _Scraper([
            403,                                            # source A, page 1
            _Response(200, _listing_html(known)),           # source B, page 1
        ])

        _crawl(scanner, [_source("A"), _source("B")], pages=1,
               skip_urls={known}, early_stop=True)

        assert scanner._last_crawl_completion_reason == "blocked"


# ── CONSUMER: does the cycle count as promotion evidence? ─────────────

class _Item:
    """Minimal MediaItem stand-in (only __dict__ attrs are serialized)."""

    def __init__(self, url, title="A Movie", status="missing", year=2024):
        self.url = url
        self.title = title
        self.status = status
        self.year = year


class _CrawlingScanner:
    """The scanner BackgroundScanner sees — with a REAL crawl inside run_scan.

    Not a hand-written stub for the crawl flags: R-1 is precisely that the
    producer and the consumer disagreed, so the test has to let the real
    ``_crawl_pages`` publish the flags and the real BackgroundScanner read them.
    Only the HTTP transport is fake. Attribute reads for the crawl outcome are
    delegated to the real ScannerService shell.
    """

    def __init__(self, outcomes, *, pages, early_stop_known=None, on_get=None):
        self._inner = _scanner_shell()
        self._inner.scraper = _Scraper(outcomes, on_get=on_get)
        self._pages = pages
        self._known = set(early_stop_known or ())
        self.scrapers = SimpleNamespace(_detail=None)
        self.calls = []

    # Crawl-outcome fields the background scanner reads, straight off the real
    # scanner. Explicit properties rather than __getattr__ so a renamed field
    # fails loudly instead of resolving to something plausible.
    @property
    def _last_crawl_seen_urls(self):
        return self._inner._last_crawl_seen_urls

    @property
    def _last_crawl_early_stopped(self):
        return self._inner._last_crawl_early_stopped

    @property
    def _last_crawl_request_count(self):
        return self._inner._last_crawl_request_count

    @property
    def _last_crawl_completion_reason(self):
        return self._inner._last_crawl_completion_reason

    def try_acquire_scan(self):
        return True

    def release_scan(self):
        return None

    def rematch_cache(self):
        return 0

    def run_scan(self, **kwargs):
        self.calls.append(kwargs)
        posts = _crawl(
            self._inner, [_source()], pages=self._pages,
            skip_urls=self._known,
            early_stop=bool(kwargs.get("early_stop")),
        )
        return [_Item(post["url"]) for post in posts]


class _BgRegistry:
    def __init__(self, config, scanner, db):
        self.config = config
        self.scanner = scanner
        self.db = db
        self.backend = SimpleNamespace(save_config=lambda: None)
        self.lifespan_generation = 1

    def owns_lifespan(self, generation):
        return generation == self.lifespan_generation


class TestPartialListingFailureIsNotPromotionEvidence:
    FEEDS = [
        {"feed": "movies_all", "outcome": "changed"},
        {"feed": "tv_all", "outcome": "not_modified"},
    ]
    P1 = "https://hdencode.org/p1/"

    def _run(self, tmp_path, monkeypatch, scanner, *, candidate_urls=None):
        _install(monkeypatch)
        monkeypatch.setattr(
            "backend.hdencode_candidate_service."
            "HDEncodeCandidateService.classify_pending",
            lambda self, **kwargs: {"processed": 0, "states": {}},
        )
        # Built outside the lambda: inside it, ``self`` is the RSS service.
        # The RSS side is given the releases the listing found, so they are
        # duplicates rather than "relevant misses" — the recorded outcome then
        # hinges only on normal_feeds_complete, which is what this fix drives.
        cycle = {
            "mode": "rss_shadow",
            "fallback_qualified": False,
            "feeds": [dict(feed) for feed in self.FEEDS],
            "candidate_urls": list(candidate_urls or ()),
            "requests": 2,
        }
        monkeypatch.setattr(
            "backend.hdencode_rss_service.HDEncodeRSSService.poll_cycle",
            lambda self, **kwargs: dict(cycle),
        )
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        cfg = {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode"],
            "background_scan_pages": 2,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            "hdencode_discovery_mode": "rss_shadow",
        }
        BackgroundScanner(_BgRegistry(cfg, scanner, db)).scan_once()
        summary = db.get_hdencode_shadow_summary()
        readiness = db.get_hdencode_rss_readiness(min_cycles=1, min_days=0)
        db.close()
        return summary, readiness

    def test_page_one_ok_page_two_blocked_does_not_count(
            self, tmp_path, monkeypatch):
        """THE BEHAVIORAL TEST the review asked for, verified at the consumer:
        the readiness query that decides promotion must not see this cycle."""
        scanner = _CrawlingScanner(
            [_Response(200, _listing_html(self.P1)), 403], pages=2)

        summary, readiness = self._run(
            tmp_path, monkeypatch, scanner, candidate_urls=[self.P1])

        assert summary["latest"]["normal_feeds_complete"] == 0
        assert summary["latest"]["outcome"] != "success"
        assert summary["successful_cycles"] == 0
        assert "insufficient_comparison_cycles" in readiness["reasons"]
        assert readiness["ready"] is False
        # The rejection is auditable: the decision record says WHY.
        assert '"listing_completion_reason": "blocked"' in (
            summary["latest"]["details_json"])

    def test_complete_crawl_still_counts(self, tmp_path, monkeypatch):
        """POSITIVE CONTROL. Both pages fetched — the control arm is sound and
        the cycle must still be usable evidence."""
        scanner = _CrawlingScanner([
            _Response(200, _listing_html(self.P1)),
            _Response(200, _listing_html("https://hdencode.org/p2/")),
        ], pages=2)

        summary, _readiness = self._run(
            tmp_path, monkeypatch, scanner,
            candidate_urls=[self.P1, "https://hdencode.org/p2/"])

        assert summary["latest"]["normal_feeds_complete"] == 1
        assert summary["latest"]["outcome"] == "success"
        assert summary["successful_cycles"] == 1
        assert '"listing_completion_reason": "complete"' in (
            summary["latest"]["details_json"])

    def test_cached_frontier_crawl_still_counts(self, tmp_path, monkeypatch):
        """POSITIVE CONTROL and DISAGREEING CASE together — the ratified
        cached_frontier arm. The background pre-cache reaches the frontier on
        nearly every steady-state cycle, so if this stops counting the promotion
        gate can never be satisfied and the "fix" has quietly killed the
        feature it was protecting."""
        known = "https://hdencode.org/already-known/"
        scanner = _CrawlingScanner(
            [_Response(200, _listing_html(known)),
             _Response(200, _listing_html("https://hdencode.org/deeper/"))],
            pages=2, early_stop_known={known})

        summary, _readiness = self._run(
            tmp_path, monkeypatch, scanner, candidate_urls=[known])

        assert scanner._last_crawl_completion_reason == "cached_frontier"
        assert summary["latest"]["normal_feeds_complete"] == 1
        assert summary["successful_cycles"] == 1

    def test_every_page_blocked_still_does_not_count(
            self, tmp_path, monkeypatch):
        """Regression guard for the earlier fix in this area (empty seen-set),
        so a reason-based rewrite cannot drop the case it replaced."""
        scanner = _CrawlingScanner([403, 403], pages=2)

        summary, _readiness = self._run(tmp_path, monkeypatch, scanner)

        assert summary["latest"]["normal_feeds_complete"] == 0
        assert summary["successful_cycles"] == 0


# ── the helper's own contract, including the legacy escape hatch ──────

class TestListingArmIncompleteContract:
    @pytest.mark.parametrize("reason,expected_incomplete", [
        ("complete", False),
        ("cached_frontier", False),
        ("blocked", True),
        ("transport_error", True),
        ("coordinator_stopped", True),
        ("cancelled", True),
        ("scan_error", True),
        ("not_started", True),
        # An unratified reason someone adds later must fail CLOSED. This is the
        # allowlist-vs-blocklist case: a blocklist of known-bad reasons would
        # call this eligible.
        ("some_future_reason", True),
    ])
    def test_only_ratified_reasons_are_eligible(self, reason,
                                                expected_incomplete):
        scanner = SimpleNamespace(
            _last_crawl_seen_urls={"https://hdencode.org/p1/"},
            _last_crawl_completion_reason=reason,
        )

        assert BackgroundScanner._listing_arm_incomplete(
            scanner, err=None) is expected_incomplete

    def test_an_empty_seen_set_is_incomplete_whatever_the_reason_says(self):
        """Both gates are required. A source that 404s every page reports
        ``complete`` truthfully — it read every page it was given — but observed
        no listing content, so it is still not a control arm."""
        scanner = SimpleNamespace(
            _last_crawl_seen_urls=set(),
            _last_crawl_completion_reason="complete",
        )

        assert BackgroundScanner._listing_arm_incomplete(
            scanner, err=None) is True

    def test_a_recorded_error_is_incomplete(self):
        scanner = SimpleNamespace(
            _last_crawl_seen_urls={"https://hdencode.org/p1/"},
            _last_crawl_completion_reason="complete",
        )

        assert BackgroundScanner._listing_arm_incomplete(
            scanner, err="Plex load failed") is True

    def test_a_double_without_the_field_falls_back_to_the_seen_set(self):
        """The escape hatch is deliberately keyed on the ATTRIBUTE BEING ABSENT.

        The real ScannerService sets the field in __init__ and re-arms it in
        every run_scan, so absence means a test stand-in, not a production
        crawl. Pre-existing doubles (plain classes, ScannerService.__new__
        shells) predate the field and must keep working; a double that sets the
        field to something unratified is judged by the allowlist, which the
        parametrized test above pins.
        """
        legacy = SimpleNamespace(
            _last_crawl_seen_urls={"https://hdencode.org/p1/"})
        assert not hasattr(legacy, "_last_crawl_completion_reason")

        assert BackgroundScanner._listing_arm_incomplete(
            legacy, err=None) is False
        assert BackgroundScanner._listing_arm_incomplete(
            SimpleNamespace(_last_crawl_seen_urls=set()), err=None) is True


# ── run_scan re-arms the reason, so no run inherits the last one ───────

def _run_scan_shell(async_body):
    """A real ScannerService cut down to what run_scan touches.

    Deliberately does NOT pre-set ``_last_crawl_completion_reason``: run_scan is
    required to seed it, which is also what keeps the consumer's legacy escape
    hatch from ever firing on a production scanner.
    """
    scanner = ScannerService.__new__(ScannerService)
    scanner._stop_event = threading.Event()
    scanner._scanning_lock = threading.Lock()
    scanner._is_scanning = False
    scanner._items_lock = threading.Lock()
    scanner.items = []
    scanner._item_counter = 0
    scanner._last_crawl_seen_urls = {"https://hdencode.org/stale/"}
    scanner._last_crawl_request_count = 0
    scanner._last_crawl_early_stopped = False
    scanner.last_scan_error = None
    scanner._log_fn = None
    scanner._progress_fn = None
    scanner.matching = SimpleNamespace(
        app=SimpleNamespace(download_history=set()))
    scanner._load_download_history = lambda: set()
    scanner._run_scan_async = async_body
    return scanner


class TestRunScanRearmsTheReason:
    def test_a_run_that_never_crawled_reports_not_started(self):
        """``_run_scan_async`` returns early in several places (no sources,
        HDEncode disabled, stop requested) without touching the crawl. Leaving
        the previous cycle's ``complete`` in place would hand the shadow
        comparison last cycle's evidence for this cycle's listing."""
        async def _body(*_args, **_kwargs):
            return None

        scanner = _run_scan_shell(_body)
        scanner._last_crawl_completion_reason = "complete"   # last cycle's

        scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert scanner._last_crawl_completion_reason == "not_started"

    def test_a_run_that_raised_reports_scan_error(self):
        """run_scan swallows the exception on purpose (raising would restore the
        purge it prevents), so the recorded reason is the only trace. Note the
        crawl may already have finished cleanly when Plex matching or enrichment
        blew up — the cycle is still not evidence."""
        async def _body(*_args, **_kwargs):
            raise RuntimeError("Plex load failed: temporary failure in name resolution")

        scanner = _run_scan_shell(_body)

        scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert scanner._last_crawl_completion_reason == "scan_error"
        assert BackgroundScanner._listing_arm_incomplete(
            scanner, err=None) is True

    def test_run_scan_seeds_the_field_even_when_nothing_sets_it(self):
        """Guards the consumer's legacy escape hatch: if a real scanner could
        reach ``_listing_arm_incomplete`` without the attribute, every
        production cycle would take the fallback path and this fix would
        evaporate."""
        async def _body(*_args, **_kwargs):
            return None

        scanner = _run_scan_shell(_body)
        assert not hasattr(scanner, "_last_crawl_completion_reason")

        scanner.run_scan(scan_type="Deep Scan", source_type="HDEncode")

        assert hasattr(scanner, "_last_crawl_completion_reason")


# ── the purge half: a cancelled crawl is as partial as an early stop ───

def _seed_aged_row(db, url="https://hdencode.org/aged/"):
    db.upsert_background_cache([{
        "url": url, "title": "Aged", "year": 2020, "status": "missing",
        "source_category": "HDEncode", "data": "{}",
    }])
    db._mutate(
        "UPDATE background_scan_cache SET last_seen_at = "
        "datetime('now','-30 days') WHERE url = ?", (url,))


class TestCancelledCrawlDoesNotPurge:
    AGED = "https://hdencode.org/aged/"

    def _config(self):
        return {
            "background_scan_enabled": True,
            "background_scan_sources": ["HDEncode"],
            "background_scan_pages": 3,
            "background_scan_retain_days": 7,
            "hdencode_enabled": True,
            # "listing" keeps the RSS branches out of the purge assertions.
            "hdencode_discovery_mode": "listing",
        }

    def test_a_crawl_cut_short_by_a_stop_leaves_the_cache_alone(
            self, tmp_path, monkeypatch):
        """The stop breaks out of the page loop, which used to set no flag at
        all — so ``purge_safe`` stayed True and rows the crawl never revisited
        were aged out of a cache nothing had refreshed."""
        _install(monkeypatch)
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db, self.AGED)
        stopper = {}
        scanner = _CrawlingScanner(
            [_Response(200, _listing_html("https://hdencode.org/p1/"))],
            pages=3,
            # Fires while page 1 is in flight, exactly as /scan/stop or a
            # shutdown would: the flag is what the page loop checks next.
            on_get=lambda _calls: stopper["scanner"].__setattr__(
                "stop_scan_flag", True),
        )
        stopper["scanner"] = scanner._inner

        BackgroundScanner(
            _BgRegistry(self._config(), scanner, db)).scan_once()

        assert scanner._last_crawl_completion_reason == "cancelled"
        assert self.AGED in db.get_background_cache_urls()
        db.close()

    def test_a_complete_crawl_still_purges(self, tmp_path, monkeypatch):
        """POSITIVE CONTROL for the change above. Retention must still work —
        a fix that holds the purge whenever anything at all happened lets the
        cache grow without bound, which is the failure mode nobody notices."""
        _install(monkeypatch)
        db = DatabaseManager(str(tmp_path / "crawler.db"))
        _seed_aged_row(db, self.AGED)
        # Three 200s for three configured pages, none of them the aged URL: if
        # the aged row survived because it was touched, the control proves
        # nothing.
        scanner = _CrawlingScanner([
            _Response(200, _listing_html("https://hdencode.org/p1/")),
            _Response(200, _listing_html("https://hdencode.org/p2/")),
            _Response(200, _listing_html("https://hdencode.org/p3/")),
        ], pages=3)

        BackgroundScanner(
            _BgRegistry(self._config(), scanner, db)).scan_once()

        assert scanner._last_crawl_completion_reason == "complete"
        assert self.AGED not in db.get_background_cache_urls()
        db.close()
