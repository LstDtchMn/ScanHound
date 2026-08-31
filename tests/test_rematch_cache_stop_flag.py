"""A re-match that could not finish must not rewrite the cache.

rematch_cache() deliberately CLEARS each item's Plex state before matching,
because the match is expected to put it back. That is safe exactly as long as
the match actually runs. When it does not, every item is sitting in the cleared
state, and persisting it turns a full library into "missing" with no Plex
match -- while logging a successful re-match.

The trigger is not exotic. stop_scan_flag is set internally whenever the
traffic coordinator reports a Cloudflare block (scanner_service, in the listing
crawl), and cleared ONLY at the top of the next run_scan. Every re-match in
between inherits it: the one after a completed download, and the one at
startup.
"""

import json
from unittest.mock import MagicMock

import pytest

from backend.scanner_service import ScannerService, ScanStatus


def cached_row(url, title, year, category="4k"):
    """A cached row shaped like the ones actually on disk.

    `category` is not decoration. Every one of the 4,073 rows in the live cache
    carries it, and it is the ONLY thing that lets a cached film resolve: the
    grammar can prove TV from a season token, but nothing proves MOVIE from a
    title, because the absence of TV evidence is not evidence of a film.

    Measured against 400 real cached rows: without the crawl category as ROUTE
    evidence, 375 of them resolve `ambiguous` and the matcher (correctly)
    refuses to route them -- so the whole cache would render
    "Type unresolved -- review" until a full re-scrape. The 25 TV rows resolved
    either way, which is the same asymmetry stated from the other side.
    """
    return {"url": url, "status": "in_library", "data": json.dumps({
        "url": url, "title": title, "year": year, "category": category,
        "status": "in_library", "status_text": "In Library", "color": "#0a0",
        "plex_info": "2160p DV", "plex_versions": '[{"res":"2160p"}]',
        "resolution": "4K", "size": "50 GB", "hdr": "HDR10", "dovi": True,
        "is_tv": False, "prior_grab": None,
    })}


ROWS = [
    cached_row("https://hdencode.org/a/", "Dune Part Two", 2024),
    cached_row("https://hdencode.org/b/", "Oppenheimer", 2023),
]


def make_service(stop_flag, have_plex=True):
    db = MagicMock()
    db.get_background_cache.return_value = [dict(r) for r in ROWS]
    db.get_downloaded_urls.return_value = set()
    db.get_downloaded_title_quality.return_value = []

    plex = MagicMock()
    plex.plex_index = {
        "all_items": ([{"title": "Dune Part Two", "year": 2024}]
                      if have_plex else []),
        "movies": [], "tv": [], "by_imdb": {}, "by_title": {},
    }

    # A matcher that genuinely finds both titles. Without this the "healthy"
    # case would blank the rows too, and a test asserting "rows were blanked"
    # would be measuring its own stub rather than the defect.
    matching = MagicMock()
    matching.find_movie_matches.return_value = (
        [{"rating_key": 101, "res": "2160p", "size": 55.0,
          "dovi": True, "hdr": True}], False)
    matching.calculate_movie_upgrade_status.return_value = (
        "IN LIBRARY", "#0a0", "2160p DV", 101)

    svc = ScannerService(
        config={"tmdb_api_key": "", "omdb_api_key": ""},
        db=db, scrapers=MagicMock(), matching=matching, plex_service=plex,
    )
    svc._log = lambda *a, **k: None
    svc._progress = lambda *a, **k: None
    svc._load_download_history = lambda: set()
    svc.stop_scan_flag = stop_flag
    return svc, db


def written(db):
    if not db.update_background_status.called:
        return []
    return [json.loads(r["data"]) for r in
            db.update_background_status.call_args[0][0]]


def test_a_healthy_rematch_still_restores_the_library_state():
    """POSITIVE CONTROL. Without this the test below proves nothing."""
    svc, db = make_service(stop_flag=False)

    svc.rematch_cache()

    rows = written(db)
    assert rows, "a healthy re-match must still write"
    assert all(r["status"] == ScanStatus.IN_LIBRARY.value for r in rows)
    assert all(r["plex_info"] == "2160p DV" for r in rows)


def test_a_stale_stop_flag_does_not_wipe_the_cache():
    svc, db = make_service(stop_flag=True)

    updated = svc.rematch_cache()

    assert updated == 0
    assert not db.update_background_status.called, (
        "the match never ran, so every item was still in its cleared state; "
        "persisting that rewrites the whole library as missing")


def test_it_does_not_report_success_for_work_it_abandoned():
    """The count is what the operator sees. It must not describe a wipe."""
    svc, db = make_service(stop_flag=True)
    assert svc.rematch_cache() == 0


def test_the_no_plex_path_is_untouched_by_this_guard():
    """With no Plex index the code never blanks, so there is nothing to
    protect -- and the download-history upgrade path must keep working."""
    svc, db = make_service(stop_flag=False, have_plex=False)

    svc.rematch_cache()

    rows = written(db)
    for r in rows:
        assert r["plex_info"] == "2160p DV", (
            "no-Plex runs must preserve cached Plex info, not blank it")


def test_a_cached_film_resolves_from_the_crawl_category():
    """The asymmetry, pinned.

    A film's title carries no positive evidence that it is a film. Only the
    category it was crawled from does. Drop that evidence and every cached
    movie becomes unroutable while every cached show still resolves.
    """
    from backend.scanner_service import ScannerService

    svc = ScannerService.__new__(ScannerService)

    film = json.loads(cached_row("https://x/f", "Oppenheimer", 2023, "4k")["data"])
    assert svc._media_item_from_dict(film).media_type == "movie"

    remux = json.loads(cached_row("https://x/r", "Heat", 1995, "remux")["data"])
    assert svc._media_item_from_dict(remux).media_type == "movie"

    show = json.loads(cached_row("https://x/s", "Some Show", 2026, "tv")["data"])
    assert svc._media_item_from_dict(show).media_type == "tv"


def test_a_cached_row_with_no_category_is_still_refused_not_guessed():
    """The fallback is the recorded route, NOT a guess. A row that never had a
    category stays unresolved, because nothing about it says which it is."""
    from backend.scanner_service import ScannerService

    svc = ScannerService.__new__(ScannerService)
    d = json.loads(cached_row("https://x/n", "Some Film", 2024)["data"])
    d.pop("category")
    assert svc._media_item_from_dict(d).media_type == "ambiguous"
