"""Every scheduled post is accounted for, through the real scan loop.

These drive the production ``_process_posts`` with its real ThreadPoolExecutor
and inject the failures a live scan actually hits. The property under test is
conservation: a post that was scheduled must end in exactly one terminal
bucket. That is what makes the counters usable -- "128 fetched, 4 kept" is only
actionable if the other 124 are each attributed to something.

The cancellation cases matter most. An operator pressing Stop strands posts in
three genuinely different states, and a scan that files them all as failures
would report a catastrophe every time someone stops a scan.
"""

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from backend.scan_metrics import (
    DiscardCode,
    TerminalKind,
    conservation_errors_of,
    media_items_emitted_of,
)
from backend.scanner_service import ScannerService


def make_service(scrape):
    """A scanner whose only real moving part is the post-processing loop."""
    svc = ScannerService(
        config={"tmdb_api_key": "", "omdb_api_key": ""},
        db=MagicMock(),
        scrapers=MagicMock(),
        matching=MagicMock(),
        plex_service=MagicMock(),
    )
    svc.scrapers.scrape_details.side_effect = scrape
    svc.download_history = set()
    svc._downloaded_titles_lookup = {}
    svc._progress = lambda *a, **k: None
    svc._log = lambda *a, **k: None
    return svc


def posts(n):
    return [
        {"url": f"https://hdencode.org/p{i}/", "type": "movie",
         "source": "hdencode", "category": "movies"}
        for i in range(n)
    ]


def good_details(url):
    return {
        "display_title": f"Movie {url[-3]}",
        "year": 2024,
        "size": "20 GB",
        "res": "4K",
        "hdr": "HDR10",
        "dovi": False,
        "rating": "8.0",
        "search_key": "movie",
        "url": url,
        "is_tv": False,
    }


def run(svc, all_posts, threads=4):
    asyncio.run(svc._process_posts(all_posts, MagicMock(), threads))
    return svc._last_scan_metrics


def assert_conserved(snap, scheduled):
    errors = conservation_errors_of(snap)
    assert errors == [], f"accounting did not balance: {errors}"
    assert snap.detail_scheduled == scheduled
    assert snap.detail_scheduled == (
        snap.detail_started + snap.detail_cancelled_before_start)


# ── the ordinary paths ───────────────────────────────────────────────

def test_an_all_success_scan_balances_and_reports_what_it_shipped():
    svc = make_service(lambda url, *a, **k: good_details(url))
    snap = run(svc, posts(6))

    assert_conserved(snap, 6)
    assert snap.media_item_created == 6
    assert media_items_emitted_of(snap) == 6
    assert len(svc.items) == 6
    assert snap.reasons == {}, "nothing was discarded, so nothing to explain"


def test_the_mostly_discarded_scan_that_motivated_this_is_no_longer_silent():
    """The production symptom: fetch many, keep few, publish as a success."""
    def scrape(url, *a, **k):
        # 2 of 10 usable; the rest come back empty, as HDEncode did.
        return good_details(url) if url.endswith(("p0/", "p1/")) else None

    svc = make_service(scrape)
    snap = run(svc, posts(10))

    assert_conserved(snap, 10)
    assert snap.media_item_created == 2
    # The 8 losses are attributed, not merely absent.
    assert snap.detail_returned_none == 8
    assert sum(snap.reasons.values()) == 8
    assert snap.reasons[DiscardCode.DETAIL_EMPTY.value] == 8, (
        "an uninstrumented falsy return is DETAIL_EMPTY -- the scraper "
        "recorded no branch, and that gap is itself the finding")


def test_a_post_that_scrapes_but_cannot_be_assembled_is_a_construction_failure():
    """Distinct from a fetch failure: the page was fine, we could not use it."""
    def scrape(url, *a, **k):
        if url.endswith("p1/"):
            return {"is_tv": False}          # too thin to build an item from
        return good_details(url)

    svc = make_service(scrape)
    svc._create_media_item = (
        lambda result: None if not result["details"].get("display_title")
        else ScannerService._create_media_item(svc, result))

    snap = run(svc, posts(3))

    assert_conserved(snap, 3)
    assert snap.media_item_construction_failed == 1
    assert snap.reasons[DiscardCode.MEDIA_ITEM_EXCEPTION.value] == 1
    assert snap.detail_returned_data == 3, "all three pages WERE read"


def test_a_worker_that_raises_is_recorded_as_having_raised():
    def scrape(url, *a, **k):
        if url.endswith("p2/"):
            raise RuntimeError("worker exploded")
        return good_details(url)

    svc = make_service(scrape)
    snap = run(svc, posts(4))

    assert_conserved(snap, 4)
    assert snap.detail_raised_exception == 1
    assert snap.media_item_created == 3
    sample = [s for s in snap.samples if s.exception_type == "RuntimeError"]
    assert len(sample) == 1
    assert sample[0].terminal_kind == TerminalKind.RAISED_EXCEPTION.value


# ── cancellation: the three states a Stop leaves behind ──────────────

def test_stopping_a_scan_is_never_reported_as_content_failure():
    """The distinction the whole taxonomy exists for.

    A Stop leaves posts cancelled-before-start, cancelled-after-start, and
    completed-but-never-consumed. None of those is the source failing or the
    parser breaking, and lumping them in would make every stopped scan look
    like an outage.
    """
    started = threading.Event()

    def scrape(url, *a, **k):
        started.set()
        return good_details(url)

    svc = make_service(scrape)

    original = svc._create_media_item

    def stop_after_first(result):
        item = original(result)
        svc.stop_scan_flag = True     # operator presses Stop mid-scan
        return item

    svc._create_media_item = stop_after_first
    snap = run(svc, posts(40), threads=2)

    assert_conserved(snap, 40)
    assert started.is_set()

    stop_related = (
        snap.detail_cancelled_before_start
        + snap.detail_cancelled_after_start
        + snap.media_item_abandoned_on_stop
    )
    assert stop_related > 0, "a Stop mid-scan must strand something"

    # The point: none of it lands in the failure buckets.
    assert snap.detail_returned_none == 0
    assert snap.detail_raised_exception == 0
    assert snap.media_item_construction_failed == 0
    assert snap.reconcile_misuse == 0, (
        "every ticket closed against a real terminal future state")
    assert snap.media_item_terminal_missing == 0
    assert snap.detail_terminal_missing == 0
    assert snap.scheduled_terminal_missing == 0


def test_a_stop_before_any_work_cancels_rather_than_failing_everything():
    svc = make_service(lambda url, *a, **k: good_details(url))
    svc.stop_scan_flag = True

    snap = run(svc, posts(12), threads=2)

    assert_conserved(snap, 12)
    assert snap.media_item_created == 0
    assert snap.detail_returned_none == 0, "nothing was tried, so nothing failed"
    assert snap.detail_raised_exception == 0
    assert (snap.detail_cancelled_before_start
            + snap.detail_cancelled_after_start) == 12


def test_a_scan_of_nothing_is_recorded_as_a_scan_of_nothing():
    svc = make_service(lambda url, *a, **k: good_details(url))
    snap = run(svc, [])

    assert snap is not None, (
        "publishing no metrics for an empty scan makes it indistinguishable "
        "from a scan that never ran")
    assert_conserved(snap, 0)
    assert media_items_emitted_of(snap) == 0


# ── the recorder must never be able to break a scan ──────────────────

def test_a_metrics_failure_does_not_fail_the_scan():
    """Bookkeeping is diagnostic. It does not get to stop the work."""
    svc = make_service(lambda url, *a, **k: good_details(url))
    svc.db.record_scan_metrics.side_effect = RuntimeError("disk full")

    snap = run(svc, posts(3))

    assert len(svc.items) == 3, "the releases still shipped"
    assert snap is not None


def test_metrics_are_persisted_once_per_pass_with_the_real_snapshot():
    svc = make_service(lambda url, *a, **k: good_details(url))
    run(svc, posts(5))

    assert svc.db.record_scan_metrics.call_count == 1
    (persisted,), _ = svc.db.record_scan_metrics.call_args
    assert persisted.detail_scheduled == 5
    assert persisted.media_item_created == 5


# ── both layers together ─────────────────────────────────────────────

def test_the_two_instrumented_layers_do_not_double_count():
    """Scanner and scraper both book started/data_returned, by design.

    The scanner cannot delegate its own facts (that broke accounting the
    moment the scraper was stubbed), and the scraper must stay self-contained
    for direct callers. So both record, and the overlap is safe only because
    those two calls are idempotent. If that ever stops being true, every count
    doubles and conservation fails -- which is what this pins, by running the
    REAL scraper underneath the REAL scan loop rather than a stub.
    """
    from unittest.mock import patch
    from backend.detail_scraper import DetailScraper
    from tests.test_detail_scraper import MockApp, MOVIE_HTML

    real = DetailScraper(MockApp())

    def http(status, body):
        r = MagicMock()
        r.status_code, r.content = status, body
        cs = MagicMock()
        cs.get.return_value = r
        return cs

    # 2 good pages, 1 that 200s with no Filename, 1 that never returns 200.
    plan = {
        "p0": (200, MOVIE_HTML),
        "p1": (200, MOVIE_HTML),
        "p2": (200, b"<html><body>no filename here</body></html>"),
        "p3": (503, b""),
    }

    def scrape(url, headers, scraper=None, **kw):
        key = url.rstrip("/").rsplit("/", 1)[-1]
        status, body = plan[key]
        return real.scrape_details(
            "https://ddlbase.com/x/", headers, http(status, body), **kw)

    svc = make_service(scrape)
    with patch("backend.detail_scraper._interruptible_sleep"):
        snap = run(svc, posts(4), threads=2)

    assert_conserved(snap, 4)
    # Each post counted exactly once despite two layers recording it.
    assert snap.detail_started == 4
    assert snap.detail_returned_data == 2
    assert snap.media_item_created == 2

    # And the specific reasons survive the trip up through the scanner --
    # not the generic DETAIL_EMPTY fallback a stub would produce.
    assert snap.reasons.get(DiscardCode.DETAIL_NO_FILENAME.value) == 1
    assert snap.reasons.get(DiscardCode.DETAIL_NO_USABLE_RESPONSE.value) == 1
    assert DiscardCode.DETAIL_EMPTY.value not in snap.reasons
    # Retries are visible: 3 attempts for the 503, 1 each for the others.
    assert snap.detail_http_requests == 6
