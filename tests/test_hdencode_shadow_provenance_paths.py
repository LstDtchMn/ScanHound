"""Production-path provenance tests: real poll_cycle, real comparison.

WHY THIS FILE EXISTS. The 2026-08-06 peer review rejected the previous fix and
said so precisely:

    The new suite calls compare_shadow() directly and injects an integer
    rss_requests. It does not drive HDEncodeRSSService.poll_cycle() and
    therefore does not test what that integer actually means.

That was correct. ``rss_requests`` is
``sum(1 for r in results if r.get("requested"))`` computed over
``normal + catchup_feeds()``, and ``poll_feed`` returns ``requested=True`` on its
exception path -- so the integer is satisfied by cases that carry no valid
normal-feed observation at all. A test that supplies the integer itself can never
discover that.

Each test here drives the real ``HDEncodeRSSService.poll_cycle()`` with per-feed
HTTP behaviour, takes the real ``feeds`` list out of the resulting cycle, and
feeds it through the real ``compare_shadow`` exactly as
``background_scanner`` does. The eight scenarios are the ones the review
enumerated, plus its required negative control.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.hdencode_shadow import compare_shadow, normal_feed_outcomes_from_results
from backend.sources.hdencode_feed_client import FeedResponse
from backend.hdencode_rss_service import HDEncodeRSSService


MOVIE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>A Film 2026 2160p WEB-DL H265 - 9 GB</title>
<link>https://hdencode.org/a-film-2026-2160p-web-h265-9-gb/</link>
<guid>https://hdencode.org/a-film-2026-2160p-web-h265-9-gb/</guid>
<pubDate>Sun, 02 Aug 2026 20:00:00 +0000</pubDate>
<category>Movies</category><description>Year: 2026</description>
</item></channel></rss>"""

TV_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>A Series S02 1080p WEB-DL H264 - 5 GB</title>
<link>https://hdencode.org/a-series-s02-1080p-web-h264-5-gb/</link>
<guid>https://hdencode.org/a-series-s02-1080p-web-h264-5-gb/</guid>
<pubDate>Sun, 02 Aug 2026 20:00:00 +0000</pubDate>
<category>TV Shows</category><description>Year: 2026</description>
</item></channel></rss>"""

# Listing rows the feed side does NOT have. One film, one series, so a partial
# cycle can be shown to discriminate between them.
# category mirrors production: scanner_service assigns "4k"/"remux" to the
# movie listings and "tv" to TV Packs. Without it a row is "unknown".
LISTING_MOVIE = {"url": "https://hdencode.org/gap-film-2026-2160p-web-x-9-gb",
                 "status": "missing", "title": "Gap Film", "season": None,
                 "category": "4k"}
LISTING_TV = {"url": "https://hdencode.org/gap-series-s03-1080p-web-y-5-gb",
              "status": "missing", "title": "Gap Series", "season": 3,
              "category": "tv"}


class Boom(Exception):
    """A transport failure raised from inside the client."""


class PerFeedClient:
    """Returns a different response per feed URL, or raises for that feed.

    ``behaviour`` maps a substring of the feed URL to a FeedResponse, or to an
    exception instance to raise. Anything unlisted returns a 304 so unrelated
    catch-up feeds stay quiet.
    """

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def fetch(self, url, *, last_modified=None):
        self.calls.append(url)
        for needle, outcome in self.behaviour.items():
            if needle in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return FeedResponse(status=304, final_url=url, last_modified="v", body=b"")


class FakeDb:
    """Minimal DB double: enough for poll_cycle, with no real persistence."""

    def __init__(self, *, stale_candidates=()):
        self.state = {}
        self.ingests = []
        self.failures = []
        self.not_modified = []
        # The persisted snapshot from an EARLIER cycle. This is the whole point:
        # list_hdencode_current_feed_urls reads the database, so it keeps
        # returning these even when this cycle's fetch failed entirely.
        self.stale_candidates = list(stale_candidates)

    def get_source_health(self):
        return {"state": "healthy"}

    def record_source_success(self, source):
        pass

    def record_source_failure(self, *a, **k):
        pass

    def get_hdencode_feed_state(self, key):
        return self.state.get(key, {})

    def list_hdencode_feed_states(self):
        return [{"feed_key": k, **v} for k, v in self.state.items()]

    def get_hdencode_rss_readiness(self, **_k):
        return {"ready": False, "reasons": ["not_under_test"]}

    def list_hdencode_current_feed_urls(self, *_a, **_k):
        return list(self.stale_candidates)

    def record_hdencode_shadow_comparison(self, **_k):
        pass

    def ingest_hdencode_feed(self, **kw):
        self.ingests.append(kw)
        return {"inserted": 0, "updated": 0}

    def record_hdencode_feed_not_modified(self, **kw):
        self.not_modified.append(kw)

    def record_hdencode_feed_failure(self, **kw):
        self.failures.append(kw)

    def update_hdencode_feed_depth(self, key, depth):
        pass


def _due(hours=3):
    return {"last_modified": "old",
            "last_checked_at": (datetime.now(timezone.utc)
                                - timedelta(hours=hours)).isoformat()}


def run_cycle(behaviour, *, due=("movies_all", "tv_all"), stale=(),
              include_catchup=False, listing=None, fresh=()):
    """Drive the real poll_cycle, then the real compare_shadow over its output.

    Returns (cycle, comparison) so a test can assert on both the provenance the
    production poll produced and the accounting it drove.
    """
    db = FakeDb(stale_candidates=stale or [
        "https://hdencode.org/unrelated-2019-1080p-old-1-gb"])
    for key in due:
        db.state[key] = _due()
    # A feed with a RECENT last_checked_at is not due, so poll_cycle skips it
    # without a request. Leaving the state absent instead would read as "never
    # checked" and therefore DUE -- which is how the first version of the
    # catch-up test accidentally polled both normal feeds and observed them.
    for key in fresh:
        db.state[key] = {"last_modified": "v",
                         "last_checked_at": datetime.now(timezone.utc).isoformat()}
    svc = HDEncodeRSSService(
        {"hdencode_enabled": True,
         "hdencode_discovery_mode": "rss_shadow",
         "hdencode_rss_poll_minutes": 60,
         "hdencode_rss_catchup_hours": 4},
        db,
        client=PerFeedClient(behaviour),
    )
    cycle = svc.poll_cycle(include_catchup=include_catchup)

    # Exactly what background_scanner does with the cycle.
    normal = {r.get("feed"): r.get("outcome") for r in cycle.get("feeds", [])
              if r.get("feed") in {"movies_all", "tv_all"}}
    comparison = compare_shadow(
        rss_urls=cycle.get("candidate_urls", []),
        listing_items=listing if listing is not None else [LISTING_MOVIE, LISTING_TV],
        rss_requests=cycle.get("requests", 0),
        listing_requests=7,
        normal_feeds_complete=(
            set(normal) == {"movies_all", "tv_all"}
            and all(o in {"changed", "not_modified"} for o in normal.values())),
        normal_feed_outcomes=normal,
    )
    return cycle, comparison


def titles(records):
    return sorted(r["title"] for r in records)


OK_MOVIE = FeedResponse(200, "https://hdencode.org/tag/movies/feed/", "v", MOVIE_RSS)
OK_TV = FeedResponse(200, "https://hdencode.org/tag/tv-shows/feed/", "v", TV_RSS)


# ── 1. both normal feeds not_due, a catch-up feed changed ──────────────────
def test_catchup_only_cycle_records_no_misses():
    """The review's first disagreeing case.

    Neither normal feed was due, so neither was observed, yet a catch-up fetch
    made rss_requests > 0. candidate_urls is entirely the persisted snapshot.
    """
    cycle, cmp_ = run_cycle({"/tag/movies-2160p/": OK_MOVIE},
                            due=(), fresh=("movies_all", "tv_all"),
                            include_catchup=True)
    outcomes = normal_feed_outcomes_from_results(cycle["feeds"])
    assert outcomes == {"movies_all": "not_due", "tv_all": "not_due"}, (
        f"both normal feeds must be skipped, got {outcomes}")
    assert cycle["requests"] >= 1, (
        "a catch-up feed was fetched, so the refuted proxy would have passed")
    assert cmp_.relevant_miss_count == 0, (
        "a catch-up request must never validate a normal-feed comparison")
    assert len(cmp_.unattributable) == 2


# ── 2. one normal feed transport failure, requested=True ───────────────────
def test_transport_failure_sets_requested_but_records_no_movie_miss():
    """requested=True on the exception path must not validate anything."""
    cycle, cmp_ = run_cycle({"/tag/movies/feed/": Boom("connection reset"),
                             "/tag/tv-shows/feed/": OK_TV})
    outcomes = normal_feed_outcomes_from_results(cycle["feeds"])
    assert outcomes["movies_all"] == "failed"
    assert cycle["requests"] >= 1, "the failed attempt still counted as a request"
    # The movie feed failed, so the movie gap is unprovable; the TV feed was
    # observed, so the TV gap is real and must still block.
    assert titles(cmp_.relevant_misses) == ["Gap Series"]
    assert titles(cmp_.unattributable) == ["Gap Film"]


# ── 3. one normal feed 500, the other changed ──────────────────────────────
def test_http_error_on_one_feed_leaves_only_the_other_attributable():
    cycle, cmp_ = run_cycle({
        "/tag/movies/feed/": FeedResponse(500, "https://hdencode.org/tag/movies/feed/", None, b""),
        "/tag/tv-shows/feed/": OK_TV})
    outcomes = normal_feed_outcomes_from_results(cycle["feeds"])
    assert outcomes["movies_all"] not in {"changed", "not_modified"}
    assert titles(cmp_.relevant_misses) == ["Gap Series"]


# ── 4. both normal feeds healthy ───────────────────────────────────────────
def test_both_feeds_healthy_counts_both_gaps():
    _, cmp_ = run_cycle({"/tag/movies/feed/": OK_MOVIE, "/tag/tv-shows/feed/": OK_TV})
    assert titles(cmp_.relevant_misses) == ["Gap Film", "Gap Series"]
    assert cmp_.unattributable == ()
    assert cmp_.outcome == "relevant_miss"


def test_both_feeds_not_modified_also_counts():
    """not_modified is a valid observation: the feed answered authoritatively."""
    nm = FeedResponse(304, "https://hdencode.org/tag/movies/feed/", "v", b"")
    nm2 = FeedResponse(304, "https://hdencode.org/tag/tv-shows/feed/", "v", b"")
    _, cmp_ = run_cycle({"/tag/movies/feed/": nm, "/tag/tv-shows/feed/": nm2})
    assert cmp_.relevant_miss_count == 2


# ── 5. valid movie feed, failed TV feed, one gap of each ───────────────────
def test_a_movie_gap_blocks_while_a_tv_gap_is_suppressed():
    """The case a cycle-level boolean cannot express, and the reason for this
    whole change: the conservative rule would discard BOTH, losing a real
    warning."""
    _, cmp_ = run_cycle({"/tag/movies/feed/": OK_MOVIE,
                         "/tag/tv-shows/feed/": Boom("timeout")})
    assert titles(cmp_.relevant_misses) == ["Gap Film"]
    assert titles(cmp_.unattributable) == ["Gap Series"]
    assert cmp_.outcome == "incomplete_feeds", (
        "the cycle is still degraded and must say so")


# ── 6. catch-up request alongside failed normal feeds ──────────────────────
def test_catchup_success_cannot_rescue_failed_normal_feeds():
    _, cmp_ = run_cycle({"/tag/movies/feed/": Boom("down"),
                         "/tag/tv-shows/feed/": Boom("down"),
                         "/tag/movies-2160p/": OK_MOVIE},
                        include_catchup=True)
    assert cmp_.relevant_miss_count == 0
    assert len(cmp_.unattributable) == 2


# ── 7. the stale candidate set is what gets compared ───────────────────────
def test_the_compared_feed_set_is_the_persisted_snapshot_when_fetches_fail():
    """Proves the mechanism, not just the outcome.

    Both fetches fail, yet candidate_urls is non-empty because
    list_hdencode_current_feed_urls reads the database. That is precisely why a
    miss from such a cycle is unprovable.
    """
    stale = ["https://hdencode.org/old-film-2019-1080p-1-gb",
             "https://hdencode.org/old-series-s01-1080p-1-gb"]
    cycle, cmp_ = run_cycle({"/tag/movies/feed/": Boom("x"),
                             "/tag/tv-shows/feed/": Boom("x")}, stale=stale)
    assert cycle["candidate_urls"] == stale, "the snapshot survived the failures"
    assert cmp_.rss_count == 2, "the comparison used the stale set"
    assert cmp_.relevant_miss_count == 0


# ── 8. the summary and grader consume the recorded cycles ──────────────────
def test_summary_reflects_attribution_end_to_end(tmp_path):
    """Record a real comparison through the real DB and read the gate back."""
    from backend.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "db.sqlite"))
    _, cmp_ = run_cycle({"/tag/movies/feed/": OK_MOVIE,
                         "/tag/tv-shows/feed/": Boom("timeout")})
    db.record_hdencode_shadow_comparison(
        cycle_uuid="c1", started_at="2026-08-02T00:00:00+00:00",
        completed_at="2026-08-02T00:00:00+00:00", metrics=cmp_.as_dict())
    summary = db.get_hdencode_shadow_summary()
    # One movie gap attributable, one TV gap suppressed.
    assert summary["relevant_misses"] == 1
    row = db.get_connection().execute(
        "SELECT normal_feed_outcomes, media_type FROM hdencode_shadow_cycles c "
        "JOIN hdencode_shadow_misses m ON m.cycle_uuid=c.cycle_uuid").fetchone()
    assert "movies_all" in (row[0] or ""), "provenance persisted"
    assert row[1] == "movie", "attribution persisted"


# ── negative control the review demanded ──────────────────────────────────
@pytest.mark.parametrize("injected", [0, 1, 2, 50])
def test_changing_only_rss_requests_cannot_validate_an_invalid_cycle(injected):
    """The explicit requirement: 'Negative controls should show that changing
    only rss_requests cannot make an invalid normal-feed comparison count.'"""
    cycle, _ = run_cycle({"/tag/movies/feed/": Boom("x"),
                          "/tag/tv-shows/feed/": Boom("x")})
    normal = normal_feed_outcomes_from_results(cycle["feeds"])
    cmp_ = compare_shadow(
        rss_urls=cycle["candidate_urls"], listing_items=[LISTING_MOVIE, LISTING_TV],
        rss_requests=injected, listing_requests=7,
        normal_feeds_complete=False, normal_feed_outcomes=normal)
    assert cmp_.relevant_miss_count == 0, (
        f"rss_requests={injected} validated a cycle where both feeds failed")


# ── the adapter the Round 2 tests stopped short of ────────────────────────────
#
# The review's Finding 1: "They do not drive a real ScannerService MediaItem, do
# not test _row_dict(), and do not test the production category='tv' signal."
# These do. A dict fixture cannot catch a field being dropped by _row_dict,
# because the dict passes straight through.

def _media_item(**kw):
    from backend.scanner_service import MediaItem
    base = dict(id="i1", title="T", year=2026, url="https://hdencode.org/x-1-gb")
    base.update(kw)
    return MediaItem(**base)


def test_row_dict_preserves_the_category_from_a_real_media_item():
    """The regression guard for the dropped field itself."""
    from backend.hdencode_shadow import _row_dict
    row = _row_dict(_media_item(category="tv"))
    assert row.get("category") == "tv", (
        "_row_dict dropped category -- attribution falls back to the slug "
        "heuristic and a genuine TV miss can be suppressed")


def test_a_real_tv_media_item_without_sNN_blocks_when_the_tv_feed_is_valid():
    """The exact false-pass the review constructed, through the real class.

    category='tv', no season, no episodes, no sNN in the URL, during a cycle
    where movies_all failed and tv_all succeeded. Before the fix this was
    attributed to movies_all and suppressed.
    """
    item = _media_item(title="Odd Show", category="tv", season=None,
                       episodes=None,
                       url="https://hdencode.org/odd-show-1080p-web-x-5-gb")
    _, cmp_ = run_cycle({"/tag/movies/feed/": Boom("down"),
                         "/tag/tv-shows/feed/": OK_TV}, listing=[item])
    assert cmp_.relevant_miss_count == 1, (
        "a real TV item was suppressed despite tv_all being observed")
    assert cmp_.relevant_misses[0]["media_type"] == "tv"
    assert "category=tv" in cmp_.relevant_misses[0]["attribution_basis"]


def test_the_same_real_tv_item_is_suppressed_when_the_tv_feed_failed():
    """The other direction: correct suppression, not blanket counting."""
    item = _media_item(title="Odd Show", category="tv", season=None,
                       episodes=None,
                       url="https://hdencode.org/odd-show-1080p-web-x-5-gb")
    _, cmp_ = run_cycle({"/tag/movies/feed/": OK_MOVIE,
                         "/tag/tv-shows/feed/": Boom("down")}, listing=[item])
    assert cmp_.relevant_miss_count == 0
    assert len(cmp_.unattributable) == 1


def test_a_real_movie_media_item_follows_its_explicit_category():
    item = _media_item(title="Odd Film", category="4k",
                       url="https://hdencode.org/odd-film-1080p-web-x-5-gb")
    _, cmp_ = run_cycle({"/tag/movies/feed/": OK_MOVIE,
                         "/tag/tv-shows/feed/": Boom("down")}, listing=[item])
    assert cmp_.relevant_miss_count == 1
    assert cmp_.relevant_misses[0]["media_type"] == "movie"


def test_a_real_search_sourced_item_needs_both_feeds():
    """No affirmative evidence -> unknown -> both feeds required."""
    item = _media_item(title="Ambiguous", category="search",
                       url="https://hdencode.org/ambiguous-2026-1080p-1-gb")
    _, half = run_cycle({"/tag/movies/feed/": OK_MOVIE,
                         "/tag/tv-shows/feed/": Boom("down")}, listing=[item])
    assert half.relevant_miss_count == 0, "unknown must not pass on one feed"
    _, both = run_cycle({"/tag/movies/feed/": OK_MOVIE,
                         "/tag/tv-shows/feed/": OK_TV}, listing=[item])
    assert both.relevant_miss_count == 1
    assert both.relevant_misses[0]["media_type"] == "unknown"
