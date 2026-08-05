"""Full-disc releases the operator excludes get a slot of their own (D-6).

`hdencode_skip_full_disc` removes full-disc releases at the LISTING stage,
before any detail request. They cost nothing and reach no parser, so they take
part in none of the detail conservation equations — but they ARE releases the
scan saw and did not ship. With no slot, a cycle that skipped forty of them is
indistinguishable from one where the listing had nothing new, which is the
exact ambiguity the scan-metrics work exists to remove.

The classification matters as much as the count. A policy exclusion is neither
a failure (the system did what it was told) nor an operator Stop (nobody
pressed anything). Filing it under either makes a correctly configured scan
misreport itself in proportion to how much the operator excluded.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from backend import scan_metrics as sm
from backend.scan_metrics import (
    DiscardCode,
    PostOutcome,
    ScanStage,
    ScanStageCounters,
    TerminalKind,
    conservation_errors_of,
    dict_of,
    outcome_groups_of,
)
from backend.scanner_service import ScannerService, is_full_disc_title


# ── the taxonomy slot itself ─────────────────────────────────────────

def test_the_code_is_staged_at_the_listing_not_at_detail():
    """It is excluded before a request is made; calling it a detail outcome
    would send an investigation to a layer that never saw it."""
    code = DiscardCode.LISTING_POLICY_EXCLUDED_FULL_DISC
    assert sm.default_stage_for(code) is ScanStage.LISTING
    assert sm.default_kind_for(code) is TerminalKind.EXCLUDED_BY_POLICY


def test_a_policy_exclusion_is_not_a_failure_and_not_a_stop():
    """DISAGREEING CASE. Both wrong classifications are individually
    plausible, and each is refuted here rather than only the one I happened
    to implement."""
    counters = ScanStageCounters()
    ticket = PostOutcome(counters, url="https://hdencode.org/bd-movie/")
    ticket.note_started()
    ticket.discard(
        DiscardCode.LISTING_POLICY_EXCLUDED_FULL_DISC,
        stage=ScanStage.LISTING,
        terminal_kind=TerminalKind.EXCLUDED_BY_POLICY,
    )

    groups = outcome_groups_of(counters.snapshot_counts())
    assert groups["policy_excluded"] == 1
    assert groups["failures"] == 0, (
        "a correctly configured exclusion policy would make the scan look "
        "broken in proportion to how much it excluded")
    assert groups["operator_stop_outcomes"] == 0, (
        "nobody pressed Stop")
    assert groups["instrumentation_gaps"] == 0


def test_the_operator_message_explains_it_without_jargon():
    msg = sm.message_for(DiscardCode.LISTING_POLICY_EXCLUDED_FULL_DISC)
    assert "full-disc" in msg.lower()
    assert "setting" in msg.lower(), (
        "the operator has to be able to tell this is their own configuration "
        "rather than something going wrong")


# ── the counter ──────────────────────────────────────────────────────

def test_the_count_survives_the_snapshot_and_the_export():
    counters = ScanStageCounters()
    counters.note_policy_excluded(7)

    snap = counters.snapshot_counts()
    assert snap.listing_policy_excluded == 7
    assert dict_of(snap)["listing_policy_excluded"] == 7


def test_policy_exclusions_do_not_disturb_detail_conservation():
    """They never reach detail, so they must stay outside its equations.

    Folding them in would inflate the denominator of every ratio about detail
    health with posts detail never saw.
    """
    counters = ScanStageCounters()
    counters.note_scheduled(3)
    counters.note_policy_excluded(40)
    for _ in range(3):
        counters.note_started()
        counters.note_detail_data()
        counters.note_item_created()

    snap = counters.snapshot_counts()
    assert conservation_errors_of(snap) == []
    assert snap.detail_scheduled == 3, "40 excluded releases are not scheduled"


def test_the_recorder_never_raises_on_a_bad_count():
    counters = ScanStageCounters()
    counters.note_policy_excluded("not a number")   # must not raise
    assert counters.listing_policy_excluded == 0
    assert counters.recorder_faults >= 1, (
        "a swallowed recorder fault must still be counted, or the bug is "
        "invisible in exactly the module built to make things visible")


# ── the real scan path ───────────────────────────────────────────────

def make_service():
    svc = ScannerService(
        config={"tmdb_api_key": "", "omdb_api_key": ""},
        db=MagicMock(), scrapers=MagicMock(), matching=MagicMock(),
        plex_service=MagicMock(),
    )
    svc.scrapers.scrape_details.side_effect = lambda url, *a, **k: {
        "display_title": "A Movie", "year": 2024, "size": "20 GB", "res": "4K",
        "hdr": "HDR10", "dovi": False, "rating": "8.0", "search_key": "a movie",
        "url": url, "is_tv": False,
    }
    svc.download_history = set()
    svc._downloaded_titles_lookup = {}
    svc._progress = lambda *a, **k: None
    svc._log = lambda *a, **k: None
    return svc


def test_the_crawls_exclusions_reach_the_published_metrics():
    """The consumer end: what the crawl excluded must appear in the row the
    scan publishes, not just in a local variable inside the crawl."""
    svc = make_service()
    svc._last_crawl_policy_excluded_observed = [
        {"url": f"https://hdencode.org/bd-{i}/"} for i in range(5)
    ]
    posts = [{"url": "https://hdencode.org/p0/", "type": "movie",
              "source": "hdencode", "category": "movies"}]

    asyncio.run(svc._process_posts(posts, MagicMock(), 2))

    snap = svc._last_scan_metrics
    assert snap.listing_policy_excluded == 5
    assert snap.media_item_created == 1
    assert conservation_errors_of(snap) == []


def test_a_scan_with_no_exclusions_reports_zero_not_stale():
    """POSITIVE CONTROL. A scan that excluded nothing must say zero — and must
    not inherit a previous crawl's number."""
    svc = make_service()
    svc._last_crawl_policy_excluded_observed = []
    posts = [{"url": "https://hdencode.org/p0/", "type": "movie",
              "source": "hdencode", "category": "movies"}]

    asyncio.run(svc._process_posts(posts, MagicMock(), 2))

    assert svc._last_scan_metrics.listing_policy_excluded == 0


def test_the_predicate_the_count_is_derived_from_still_holds():
    """Guard the input to all of the above: if is_full_disc_title drifts, the
    count silently measures something else."""
    assert is_full_disc_title("[BD] Some Movie 2024") is True
    assert is_full_disc_title("BD Movie Title") is False, (
        "an unbracketed 'BD' is an ordinary release")
    assert is_full_disc_title("Some BDRip Movie") is False
    assert is_full_disc_title(None) is False
