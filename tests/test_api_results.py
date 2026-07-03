"""Tests for server-side results filtering, sorting, pagination."""
import pytest

from backend.api.routes.results import (
    _filter_and_sort, _effective_category, _has_plex_copy,
    _parse_size_to_bytes, _parse_posted_date,
)


@pytest.fixture(autouse=True)
def _reset_registry_db():
    """Reset the module-level registry.db (and selection state) after each test.

    Several tests in this file set ``registry.db = MagicMock()`` directly
    (bypassing the ``client`` fixture in test_api_routes.py), so without this
    the mock would leak into whichever test runs next in the same session.
    """
    from backend.api.routes import results as results_mod
    with results_mod._cache_parse_lock:
        results_mod._cache_parse_cache["version"] = None
        results_mod._cache_parse_cache["items"] = []
        results_mod._cache_parse_cache["last_updated"] = None
    yield
    from backend.api.dependencies import registry
    registry.db = None
    from backend.api.routes.results import _selected, _selected_lock
    with _selected_lock:
        _selected.clear()
    with results_mod._cache_parse_lock:
        results_mod._cache_parse_cache["version"] = None
        results_mod._cache_parse_cache["items"] = []
        results_mod._cache_parse_cache["last_updated"] = None


def _it(**kw):
    base = dict(title="A", status="missing", category=None, season=None,
                genres=[], language="English", resolution="1080p", hdr="",
                dovi=False, plex_versions="[]", year=2020, rating=5.0,
                size="4.5 GB", posted_date="June 8, 2026 at 12:56 AM",
                group_key="a-2020")
    base.update(kw)
    return base


def test_effective_category_rules():
    assert _effective_category(_it(category="remux")) == "remux"
    assert _effective_category(_it(category=None, season=2)) == "tv"
    assert _effective_category(_it(category=None, season=None)) == "4k"


def test_category_filter_shows_enabled_and_unknown():
    items = [_it(title="M", category="remux"), _it(title="T", season=1),
             _it(title="S", category="search")]
    out = _filter_and_sort(items, category=["4k"])
    titles = {i["title"] for i in out}
    assert titles == {"S"}  # remux+tv hidden; unknown 'search' always shows


def test_quick_inplex_and_hdrdv():
    inplex = _it(title="P", plex_versions='[{"v":1}]')
    dv = _it(title="D", dovi=True)
    plain = _it(title="X")
    assert {i["title"] for i in _filter_and_sort([inplex, dv, plain], quick=["inplex"])} == {"P"}
    assert {i["title"] for i in _filter_and_sort([inplex, dv, plain], quick=["hdrdv"])} == {"D"}


def test_typed_sort_size_and_posted():
    a = _it(title="A", size="9 GB", posted_date="June 8, 2026 at 12:00 AM")
    b = _it(title="B", size="10 GB", posted_date="July 3, 2026 at 12:00 AM")
    by_size = _filter_and_sort([a, b], sort="size", order="desc")
    assert [i["title"] for i in by_size] == ["B", "A"]  # 10GB > 9GB (not lexical)
    by_posted = _filter_and_sort([a, b], sort="posted_date", order="desc")
    assert [i["title"] for i in by_posted] == ["B", "A"]  # July after June


def test_filter_missing_keeps_only_missing_status():
    missing = _it(title="M", status="Missing")
    have = _it(title="H", status="library")
    out = _filter_and_sort([missing, have], filter="missing")
    assert {i["title"] for i in out} == {"M"}


def test_search_matches_title_substring_case_insensitively():
    match = _it(title="The XYZ Movie")
    nomatch = _it(title="Something Else")
    out = _filter_and_sort([match, nomatch], search="xyz")
    assert {i["title"] for i in out} == {"The XYZ Movie"}


def test_genre_filter_keeps_only_intersecting_items():
    action = _it(title="A", genres=["Action", "Thriller"])
    drama = _it(title="D", genres=["Drama"])
    none_genre = _it(title="N", genres=[])
    out = _filter_and_sort([action, drama, none_genre], genre=["Action"])
    assert {i["title"] for i in out} == {"A"}


def test_language_filter_keeps_only_matching_language():
    fr = _it(title="F", language="French")
    en = _it(title="E", language="English")
    out = _filter_and_sort([fr, en], language=["French"])
    assert {i["title"] for i in out} == {"F"}


def test_sort_title_casefold_orders_apple_before_banana():
    apple = _it(title="apple")
    banana = _it(title="Banana")
    out = _filter_and_sort([banana, apple], sort="title", order="asc")
    assert [i["title"] for i in out] == ["apple", "Banana"]


def test_sort_year_numeric_desc():
    old = _it(title="Old", year=1999)
    new = _it(title="New", year=2024)
    out = _filter_and_sort([old, new], sort="year", order="desc")
    assert [i["title"] for i in out] == ["New", "Old"]


def test_sort_rating_numeric_desc():
    low = _it(title="Low", rating=2.0)
    high = _it(title="High", rating=9.0)
    out = _filter_and_sort([low, high], sort="rating", order="desc")
    assert [i["title"] for i in out] == ["High", "Low"]


def test_parse_size_to_bytes_failsafe_branches():
    assert _parse_size_to_bytes("") == 0.0
    assert _parse_size_to_bytes("garbage") == 0.0
    # Regex allows multiple dots ("[\d.]+"), so this reaches float() and must
    # not raise ValueError -- guarded to fail safe and return 0.0.
    assert _parse_size_to_bytes("..5 GB") == 0.0


def test_parse_posted_date_failsafe_branches():
    assert _parse_posted_date("") == 0.0
    assert _parse_posted_date("not a date") == 0.0


def test_has_plex_copy_failsafe_branches():
    assert _has_plex_copy(_it(plex_versions="{bad json")) is False
    assert _has_plex_copy(_it(plex_versions=None)) is False


import json
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from backend.api.main import create_app
from backend.api.dependencies import registry


def _row(title, status="missing", category="4k", **kw):
    data = dict(title=title, status=status, category=category, url=f"u/{title}",
                group_key=f"{title}-k", season=None, genres=[], language="English",
                resolution="4K", hdr="", dovi=False, plex_versions="[]",
                year=2020, rating=5.0, size="4.5 GB",
                posted_date="June 8, 2026 at 12:56 AM")
    data.update(kw)
    return {"url": data["url"], "data": json.dumps(data), "last_seen_at": "2026-06-30T00:00:00"}


def _client_with_cache(rows, version=None):
    registry.db = MagicMock()
    registry.db.get_background_cache.return_value = rows
    registry.db.get_dismissed_urls.return_value = set()
    # A real (count, max_last_seen_at)-shaped version by default so the B2
    # parse-cache in results.py behaves like it would against a real
    # DatabaseManager (unchanged rows -> unchanged version -> cache hit).
    if version is None:
        max_seen = max((r.get("last_seen_at") for r in rows), default=None)
        version = (len(rows), max_seen)
    registry.db.get_background_cache_version.return_value = version
    # Auth is gated on registry.auth_nonce OR db.has_password(); a bare
    # MagicMock() makes has_password() truthy by default, which would 401
    # every request in this TestClient (no lifespan/token involved here).
    registry.db.has_password.return_value = False
    return TestClient(create_app())


def test_cached_stats_whole_set_but_filtered_narrows():
    rows = [_row("A", status="missing"), _row("B", status="in_library"),
            _row("C", status="missing")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"filter": "missing", "per_page": 100}).json()
    assert r["stats"]["total"] == 3          # whole visible set
    assert r["stats"]["missing"] == 2
    assert r["total"] == 2                    # after status filter
    assert {i["title"] for i in r["items"]} == {"A", "C"}


def test_cached_pages_are_disjoint_and_cover_full_set():
    rows = [_row(f"T{n:03d}") for n in range(250)]
    c = _client_with_cache(rows)
    seen = []
    for page in (1, 2, 3):
        r = c.get("/results/cached", params={"per_page": 100, "page": page,
                                             "sort": "title", "order": "asc"}).json()
        seen.extend(i["title"] for i in r["items"])
        assert r["total"] == 250
    assert len(seen) == 250 and len(set(seen)) == 250


def test_cached_title_counts_sum_to_total():
    rows = [_row("Dup"), _row("Dup", url="u/dup2", group_key="Dup-k2"), _row("Solo")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"per_page": 100}).json()
    assert r["title_counts"]["Dup"] == 2
    assert sum(r["title_counts"].values()) == r["total"]


def test_cached_category_query_param_filters():
    rows = [_row("K", category="4k"), _row("R", category="remux")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"category": "4k", "per_page": 100}).json()
    assert {i["title"] for i in r["items"]} == {"K"}


def test_cached_per_page_500_is_accepted():
    """Live mode has no client-side pagination, so a reloaded live set needs to
    fetch up to 500 rows in one request. Guards against the cap regressing to
    the smaller 200 limit that made items 201+ unreachable on reload."""
    rows = [_row(f"T{n:03d}") for n in range(10)]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"per_page": 500})
    assert r.status_code == 200


def test_cached_per_page_501_is_rejected():
    rows = [_row("A")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"per_page": 501})
    assert r.status_code == 422


def test_select_all_filtered_returns_matching_group_keys():
    rows = [_row("A", status="missing", category="4k"),
            _row("B", status="in_library", category="4k"),
            _row("C", status="missing", category="remux")]
    c = _client_with_cache(rows)
    r = c.post("/results/select-all",
               json={"source": "cache", "filter": "missing", "category": "4k"}).json()
    assert r["selected_count"] == 1
    assert r["group_keys"] == ["A-k"]


# ── posted_after / posted_before date-range filter ────────────────────────

def test_posted_after_excludes_items_before_the_bound():
    early = _it(title="Early", posted_date="June 1, 2026 at 12:00 AM")
    late = _it(title="Late", posted_date="June 20, 2026 at 12:00 AM")
    out = _filter_and_sort([early, late], posted_after="2026-06-08")
    assert {i["title"] for i in out} == {"Late"}


def test_posted_before_excludes_items_after_the_bound_inclusive_of_end_of_day():
    early = _it(title="Early", posted_date="June 1, 2026 at 12:00 AM")
    boundary_late = _it(title="BoundaryLate", posted_date="June 8, 2026 at 11:59 PM")
    late = _it(title="Late", posted_date="June 20, 2026 at 12:00 AM")
    out = _filter_and_sort([early, boundary_late, late], posted_before="2026-06-08")
    assert {i["title"] for i in out} == {"Early", "BoundaryLate"}


def test_posted_after_and_before_both_bounds_inclusive_on_boundary_dates():
    start_boundary = _it(title="StartBoundary", posted_date="June 8, 2026 at 12:00 AM")
    end_boundary = _it(title="EndBoundary", posted_date="June 10, 2026 at 11:30 PM")
    outside_before = _it(title="TooEarly", posted_date="June 7, 2026 at 11:59 PM")
    outside_after = _it(title="TooLate", posted_date="June 11, 2026 at 12:00 AM")
    out = _filter_and_sort(
        [start_boundary, end_boundary, outside_before, outside_after],
        posted_after="2026-06-08", posted_before="2026-06-10",
    )
    assert {i["title"] for i in out} == {"StartBoundary", "EndBoundary"}


def test_missing_posted_date_excluded_when_a_bound_is_set():
    dateless = _it(title="Dateless", posted_date="")
    dated = _it(title="Dated", posted_date="June 8, 2026 at 12:00 AM")
    out = _filter_and_sort([dateless, dated], posted_after="2026-06-01")
    assert {i["title"] for i in out} == {"Dated"}


def test_missing_posted_date_included_when_no_bound_is_set():
    dateless = _it(title="Dateless", posted_date="")
    dated = _it(title="Dated", posted_date="June 8, 2026 at 12:00 AM")
    out = _filter_and_sort([dateless, dated])
    assert {i["title"] for i in out} == {"Dateless", "Dated"}


def test_bad_posted_after_format_returns_422_on_cached_endpoint():
    rows = [_row("A")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"posted_after": "06/08/2026"})
    assert r.status_code == 422


def test_bad_posted_before_format_returns_422_on_live_endpoint():
    rows = [_row("A")]
    c = _client_with_cache(rows)
    r = c.get("/results", params={"posted_before": "not-a-date"})
    assert r.status_code == 422


def test_cached_posted_range_narrows_total_and_title_counts_but_stats_stay_whole_set():
    rows = [
        _row("Early", posted_date="June 1, 2026 at 12:00 AM"),
        _row("Mid", posted_date="June 8, 2026 at 12:00 AM"),
        _row("Late", posted_date="June 20, 2026 at 12:00 AM"),
    ]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"posted_after": "2026-06-05", "posted_before": "2026-06-10"}).json()
    assert r["total"] == 1
    assert r["title_counts"] == {"Mid": 1}
    assert r["stats"]["total"] == 3  # whole visible set, unaffected by the date range


def test_select_all_respects_posted_range():
    rows = [
        _row("Early", posted_date="June 1, 2026 at 12:00 AM"),
        _row("Mid", posted_date="June 8, 2026 at 12:00 AM"),
    ]
    c = _client_with_cache(rows)
    r = c.post("/results/select-all",
               json={"source": "cache", "posted_after": "2026-06-05"}).json()
    assert r["group_keys"] == ["Mid-k"]

def test_cached_posted_calendar_invalid_date_422():
    """Regex-valid but calendar-invalid dates (2026-02-31) must 422, not 500."""
    rows = [_row("A")]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"posted_after": "2026-02-31"})
    assert r.status_code == 422
    assert "calendar" in r.json()["detail"].lower() or "Invalid" in r.json()["detail"]
    r2 = c.get("/results/cached", params={"posted_before": "2025-13-01"})
    assert r2.status_code == 422


# ── B2: server facets (available_genres / available_languages) ───────────

def test_cached_facets_reflect_full_set_not_page():
    rows = [
        _row("A", genres=["Action", "Thriller"], language="English"),
        _row("B", genres=["Drama"], language="French"),
        _row("C", genres=["Action"], language="English"),
    ]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"per_page": 1, "page": 1}).json()
    # Only 1 item returned on the page, but facets must cover the whole set.
    assert len(r["items"]) == 1
    assert r["available_genres"] == ["Action", "Drama", "Thriller"]
    assert r["available_languages"] == ["English", "French"]


def test_cached_facets_unaffected_by_filters():
    rows = [
        _row("A", status="missing", genres=["Action"], language="English"),
        _row("B", status="in_library", genres=["Drama"], language="French"),
    ]
    c = _client_with_cache(rows)
    r = c.get("/results/cached", params={"filter": "missing"}).json()
    assert r["total"] == 1  # filter narrows the page/total...
    # ...but facets still reflect the whole (dismissal-filtered) visible set.
    assert r["available_genres"] == ["Action", "Drama"]
    assert r["available_languages"] == ["English", "French"]


def test_cached_facets_deduplicated_and_sorted():
    rows = [
        _row("A", genres=["Zeta", "Action"], language="English"),
        _row("B", genres=["Action"], language="English"),
    ]
    c = _client_with_cache(rows)
    r = c.get("/results/cached").json()
    assert r["available_genres"] == ["Action", "Zeta"]
    assert r["available_languages"] == ["English"]


def test_cached_facets_empty_genres_and_missing_language_ignored():
    rows = [
        _row("A", genres=[], language=""),
        _row("B", genres=["Comedy"], language="German"),
    ]
    c = _client_with_cache(rows)
    r = c.get("/results/cached").json()
    assert r["available_genres"] == ["Comedy"]
    assert r["available_languages"] == ["German"]


def test_live_results_do_not_include_facets():
    """B2 facets are only added to /results/cached, not the live endpoint."""
    registry.db = MagicMock()
    registry.db.get_dismissed_urls.return_value = set()
    registry.db.has_password.return_value = False
    c = TestClient(create_app())
    r = c.get("/results").json()
    assert "available_genres" not in r
    assert "available_languages" not in r


def test_cached_paging_does_not_change_facets_or_totals():
    rows = [_row(f"T{n:03d}", genres=["Action"] if n % 2 == 0 else ["Drama"],
                 language="English") for n in range(50)]
    c = _client_with_cache(rows)
    r1 = c.get("/results/cached", params={"per_page": 10, "page": 1}).json()
    r2 = c.get("/results/cached", params={"per_page": 10, "page": 2}).json()
    assert r1["total"] == r2["total"] == 50
    assert r1["available_genres"] == r2["available_genres"] == ["Action", "Drama"]
    assert r1["available_languages"] == r2["available_languages"] == ["English"]


# ── B2: parse-cache (avoid re-parsing all cached JSON blobs every request) ─

def test_cached_reuses_parsed_items_when_version_unchanged(monkeypatch):
    rows = [_row("A"), _row("B")]
    c = _client_with_cache(rows)

    row_blobs = {r["data"] for r in rows}
    parse_calls = []
    real_loads = json.loads

    def spy_loads(s, *a, **kw):
        if isinstance(s, str) and s in row_blobs:
            parse_calls.append(s)
        return real_loads(s, *a, **kw)

    monkeypatch.setattr(json, "loads", spy_loads)

    r1 = c.get("/results/cached").json()
    calls_after_first = len(parse_calls)
    assert calls_after_first == 2  # one json.loads() per cached row, first request
    r2 = c.get("/results/cached").json()

    assert r1["total"] == r2["total"] == 2
    # Second request must not re-parse the 2 cached JSON blobs (version
    # unchanged -> served from the parse cache).
    assert len(parse_calls) == calls_after_first, (
        "results/cached re-parsed row JSON on a request where the "
        "background cache version hadn't changed"
    )


def test_cached_reparses_when_version_changes():
    rows_v1 = [_row("A")]
    c = _client_with_cache(rows_v1, version=(1, "2026-06-30T00:00:00"))
    r1 = c.get("/results/cached").json()
    assert r1["total"] == 1

    # Simulate a re-scrape: new row set + bumped version.
    rows_v2 = [_row("A"), _row("B")]
    registry.db.get_background_cache.return_value = rows_v2
    registry.db.get_background_cache_version.return_value = (2, "2026-06-30T00:01:00")

    r2 = c.get("/results/cached").json()
    assert r2["total"] == 2
