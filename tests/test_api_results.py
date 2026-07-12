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


def test_resolution_facet_is_or_combined_and_movies_only():
    uhd = _it(title="U", resolution="4K", category="4k", season=None)
    hd = _it(title="H", resolution="1080p", category="remux", season=None)
    tv = _it(title="T", resolution="4K", category="tv", season=1)  # a 4K TV show
    # 4K/1080p are MOVIES ONLY — a 4K TV show must NOT appear under '4K'.
    assert {i["title"] for i in _filter_and_sort([uhd, hd, tv], resolution=["4K"])} == {"U"}
    assert {i["title"] for i in _filter_and_sort([uhd, hd, tv], resolution=["1080p"])} == {"H"}
    # 'TV' keys off effective category, regardless of the show's resolution.
    assert {i["title"] for i in _filter_and_sort([uhd, hd, tv], resolution=["TV"])} == {"T"}
    # OR within the set: 4K + 1080p shows only the movies (no TV leakage).
    assert {i["title"] for i in _filter_and_sort([uhd, hd, tv], resolution=["4K", "1080p"])} == {"U", "H"}
    # 4K + TV shows the 4K movie AND the TV show.
    assert {i["title"] for i in _filter_and_sort([uhd, hd, tv], resolution=["4K", "TV"])} == {"U", "T"}
    # No filter shows everything.
    assert len(_filter_and_sort([uhd, hd, tv])) == 3


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


def test_filter_and_sort_genre_exclude_hides_matching_items():
    a = _it(title="A", genres=["Comedy"])
    b = _it(title="B", genres=["Reality"])
    c = _it(title="C", genres=["Reality", "Comedy"])
    d = _it(title="D", genres=[])
    out = _filter_and_sort([a, b, c, d], genre_exclude=["Reality"])
    assert {i["title"] for i in out} == {"A", "D"}


def test_filter_and_sort_genre_include_and_exclude_combined():
    a = _it(title="A", genres=["Comedy"])
    b = _it(title="B", genres=["Comedy", "Reality"])
    c = _it(title="C", genres=["Drama"])
    out = _filter_and_sort([a, b, c], genre=["Comedy"], genre_exclude=["Reality"])
    assert {i["title"] for i in out} == {"A"}


def test_filter_and_sort_genre_exclude_never_hides_genre_less_items():
    no_genres = _it(title="NoGenres", genres=[])
    no_genres_key = _it(title="NoGenresKey")
    del no_genres_key["genres"]
    out = _filter_and_sort([no_genres, no_genres_key], genre_exclude=["Reality"])
    assert {i["title"] for i in out} == {"NoGenres", "NoGenresKey"}


def test_filter_and_sort_genre_include_only_regression_unchanged():
    """Existing include-only behavior must be byte-identical."""
    a = _it(title="A", genres=["Comedy"])
    b = _it(title="B", genres=["Drama"])
    out = _filter_and_sort([a, b], genre=["Comedy"])
    assert [i["title"] for i in out] == ["A"]


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
    registry.db.get_dismissed_title_quality.return_value = []
    registry.db.get_downloaded_urls.return_value = set()
    registry.db.get_downloaded_title_quality.return_value = []
    registry.db.get_downloaded_titles.return_value = []
    registry.db.list_bookmark_keys.return_value = set()
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


def test_downloaded_urls_overlay_marks_downloaded_at_read_time():
    # Cache still says 'missing' (it predates the grab); the central downloads
    # table knows the URL was grabbed. The read overlay must mark it downloaded.
    rows = [_row("A", status="missing"), _row("B", status="missing")]
    c = _client_with_cache(rows)
    registry.db.get_downloaded_urls.return_value = {"u/A"}
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_title = {i["title"]: i for i in data["items"]}
    assert by_title["A"]["status"] == "downloaded"   # overlaid from central DB
    assert by_title["B"]["status"] == "missing"       # untouched
    # And it counts toward the 'downloaded' status filter without a re-scan.
    only_dl = c.get("/results/cached", params={"filter": "downloaded", "per_page": 100}).json()
    assert {i["title"] for i in only_dl["items"]} == {"A"}


def test_grabbed_sibling_versions_are_reclassified():
    # Same title/group "Dune", three release URLs at different resolutions, all
    # 'missing' in the cache. You grabbed the 1080p one.
    rows = [
        _row("Dune", url="d/1080", resolution="1080p", group_key="dune|2021"),
        _row("Dune", url="d/4k", resolution="4K", group_key="dune|2021"),
        _row("Dune", url="d/720", resolution="720p", group_key="dune|2021"),
    ]
    c = _client_with_cache(rows)
    registry.db.get_downloaded_urls.return_value = {"d/1080"}
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert by_url["d/1080"]["status"] == "downloaded"          # the exact grab
    assert by_url["d/720"]["status"] == "downloaded_similar"   # lower res → have a copy
    # Higher-res sibling stays grabbable + annotated (NOT dumped in Upgrades tab).
    assert by_url["d/4k"]["status"] == "missing"
    assert by_url["d/4k"]["prior_grab"]["resolution"] == "1080p"
    assert by_url["d/720"]["prior_grab"]["resolution"] == "1080p"
    # Downloaded/similar leave the deck; the better 4K stays counted as missing.
    assert data["stats"]["upgrade"] == 0


def test_shape_results_annotates_bookmarked_flag():
    # One item matches a bookmark by imdb_id, one by the title/year/media_type
    # fallback key, one is not bookmarked at all.
    rows = [
        _row("Dune: Part Two", url="u/imdb", group_key="dune2-2020", imdb_id="tt1234567"),
        _row("Some Obscure Show", url="u/title", group_key="show-2020", imdb_id=None, season=1),
        _row("Not Bookmarked", url="u/plain", group_key="plain-2020"),
    ]
    c = _client_with_cache(rows)
    registry.db.list_bookmark_keys.return_value = {
        ("imdb", "tt1234567"),
        ("title", "some obscure show", 2020, "tv"),
    }
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_title = {i["title"]: i for i in data["items"]}
    assert by_title["Dune: Part Two"]["bookmarked"] is True
    assert by_title["Some Obscure Show"]["bookmarked"] is True
    assert by_title["Not Bookmarked"]["bookmarked"] is False


def test_sibling_match_is_year_aware_no_remake_contamination():
    # Two different "Dune" movies (1984 vs 2021) → different group_keys. Grabbing
    # the 2021 one must NOT reclassify the 1984 one.
    rows = [
        _row("Dune", url="d/2021", resolution="1080p", group_key="dune|2021"),
        _row("Dune", url="d/1984", resolution="1080p", group_key="dune|1984"),
    ]
    c = _client_with_cache(rows)
    registry.db.get_downloaded_urls.return_value = {"d/2021"}
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert by_url["d/2021"]["status"] == "downloaded"
    assert by_url["d/1984"]["status"] == "missing"   # different film, untouched


def test_downloaded_overlay_survives_cache_rotation():
    # The grabbed URL has ROLLED OUT of the background cache (early-stop keeps
    # only recent pages). The downloads table still knows the title+year+season
    # and quality — siblings must stay reclassified, not resurface as missing.
    rows = [
        _row("Dune", url="d/4k", resolution="4K", group_key="dune|2021|S0"),
        _row("Dune", url="d/720", resolution="720p", group_key="dune|2021|S0"),
    ]
    c = _client_with_cache(rows)
    registry.db.get_downloaded_urls.return_value = set()      # URL gone from cache
    registry.db.get_downloaded_title_quality.return_value = [
        ("dune", 2021, None, "1080p", 0),                      # the recorded grab
    ]
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert by_url["d/720"]["status"] == "downloaded_similar"   # lower → have a copy
    assert by_url["d/4k"]["status"] == "missing"               # better → still grabbable
    assert by_url["d/4k"]["prior_grab"]["resolution"] == "1080p"


def test_grabbed_tv_sibling_reclassified_by_title_quality():
    # TV overlay regression: served TV items carry the uniform key
    # "{title}|{year}|S{season}" (e.g. "dune|2021|S1") assigned by the scanner's
    # _assign_group_keys after enrichment. The title-quality overlay must
    # reconstruct that SAME format so a grabbed TV sibling stays reclassified
    # after the grabbed URL rolls out of the background cache.
    rows = [
        _row("Dune", season=1, group_key="dune|2021|S1", resolution="4K", url="d/s1-4k"),
        _row("Dune", season=1, group_key="dune|2021|S1", resolution="720p", url="d/s1-720"),
    ]
    c = _client_with_cache(rows)
    registry.db.get_downloaded_urls.return_value = set()       # grabbed URL gone from cache
    registry.db.get_downloaded_title_quality.return_value = [
        ("dune", 2021, 1, "1080p", 0),                          # grabbed S1 (2021) at 1080p
    ]
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert by_url["d/s1-720"]["status"] == "downloaded_similar"  # lower → have a copy
    assert by_url["d/s1-4k"]["status"] == "missing"              # better → still grabbable
    assert by_url["d/s1-4k"]["prior_grab"]["resolution"] == "1080p"


def test_dismiss_without_meta_backfills_from_cache():
    # An old app bundle sends urls+titles but no meta. The server must fill
    # group_key/resolution/dovi from its own cached item so title-level skip
    # still works.
    rows = [_row("Heat", url="h/1080", resolution="1080p",
                 group_key="heat|1995|S0", dovi=False)]
    c = _client_with_cache(rows)
    captured = {}
    def _capture(rows_iter):
        captured["rows"] = list(rows_iter)
        return True
    registry.db.add_dismissed_items.side_effect = _capture
    r = c.post("/results/dismiss",
               json={"urls": ["h/1080"], "titles": {"h/1080": "Heat"},
                     "dismissed": True})
    assert r.status_code == 200
    (url, title, gk, res, dovi), = captured["rows"]
    assert url == "h/1080" and gk == "heat|1995|S0" and res == "1080p"


def test_skipped_title_hides_same_or_lower_keeps_upgrade():
    # User swiped-left ("skip") the title at 1080p. A 1080p re-upload and a 720p
    # re-encode must stay hidden; only a genuine 4K upgrade may resurface.
    rows = [
        _row("Heat", url="h/1080", resolution="1080p", group_key="heat|1995"),
        _row("Heat", url="h/720", resolution="720p", group_key="heat|1995"),
        _row("Heat", url="h/4k", resolution="4K", group_key="heat|1995"),
    ]
    c = _client_with_cache(rows)
    # No exact URL is dismissed (the originally-skipped release rolled off); the
    # title-level threshold is what gates the re-uploads.
    registry.db.get_dismissed_title_quality.return_value = [("heat|1995", "1080p", 0)]
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert "h/1080" not in by_url   # same res as skipped → hidden
    assert "h/720" not in by_url    # lower res → hidden
    assert "h/4k" in by_url          # genuine upgrade → resurfaces


def test_skipped_title_dv_gain_at_same_resolution_resurfaces():
    # Skipped a 4K non-DV release; a 4K Dolby Vision version is an upgrade.
    rows = [
        _row("Sicario", url="s/4k", resolution="4K", dovi=False, group_key="sicario|2015"),
        _row("Sicario", url="s/4kdv", resolution="4K", dovi=True, group_key="sicario|2015"),
    ]
    c = _client_with_cache(rows)
    registry.db.get_dismissed_title_quality.return_value = [("sicario|2015", "4K", 0)]
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert "s/4k" not in by_url     # same res, no DV gain → hidden
    assert "s/4kdv" in by_url        # DV gain at same res → resurfaces


def test_sibling_dv_gain_at_same_resolution_stays_grabbable():
    # Grabbed 4K SDR; a 4K Dolby Vision sibling is a real upgrade → must stay
    # missing (grabbable), not be hidden as downloaded_similar.
    rows = [
        _row("Blade", url="b/sdr", resolution="4K", dovi=False, group_key="blade|2020"),
        _row("Blade", url="b/dv", resolution="4K", dovi=True, group_key="blade|2020"),
    ]
    c = _client_with_cache(rows)
    registry.db.get_downloaded_urls.return_value = {"b/sdr"}
    data = c.get("/results/cached", params={"per_page": 100}).json()
    by_url = {i["url"]: i for i in data["items"]}
    assert by_url["b/sdr"]["status"] == "downloaded"
    assert by_url["b/dv"]["status"] == "missing"     # DV gain → still worth grabbing
    assert by_url["b/dv"]["prior_grab"]["resolution"] == "4K"


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


# ── B3: _selected stays bounded under many selects ─────────────────────

def test_selected_set_stays_bounded_under_many_selects():
    from backend.api.routes import results as results_mod
    c = _client_with_cache([_row("A")])
    over_cap = results_mod._MAX_SELECTED + 500
    for batch_start in range(0, over_cap, 500):
        keys = [f"k-{n}" for n in range(batch_start, batch_start + 500)]
        r = c.post("/results/select", json={"group_keys": keys, "selected": True})
        assert r.status_code == 200
    assert r.json()["selected_count"] <= results_mod._MAX_SELECTED
    with results_mod._selected_lock:
        assert len(results_mod._selected) <= results_mod._MAX_SELECTED


def test_selected_set_evicts_oldest_first():
    from backend.api.routes import results as results_mod
    c = _client_with_cache([_row("A")])
    with results_mod._selected_lock:
        results_mod._selected.clear()
    # Fill to exactly the cap.
    keys = [f"k-{n}" for n in range(results_mod._MAX_SELECTED)]
    c.post("/results/select", json={"group_keys": keys, "selected": True})
    with results_mod._selected_lock:
        assert "k-0" in results_mod._selected
    # One more selection should evict the oldest ("k-0"), not truncate arbitrarily.
    c.post("/results/select", json={"group_keys": ["k-new"], "selected": True})
    with results_mod._selected_lock:
        assert len(results_mod._selected) == results_mod._MAX_SELECTED
        assert "k-0" not in results_mod._selected
        assert "k-new" in results_mod._selected


def test_selected_set_reselecting_moves_to_end_not_evicted():
    """Re-selecting an already-selected key must not make it the eviction
    candidate ahead of keys that were never touched again."""
    from backend.api.routes import results as results_mod
    c = _client_with_cache([_row("A")])
    with results_mod._selected_lock:
        results_mod._selected.clear()
    keys = [f"k-{n}" for n in range(results_mod._MAX_SELECTED)]
    c.post("/results/select", json={"group_keys": keys, "selected": True})
    # Re-select the oldest key -- it should move to the end, so the NEXT
    # oldest ("k-1") becomes the eviction candidate instead.
    c.post("/results/select", json={"group_keys": ["k-0"], "selected": True})
    c.post("/results/select", json={"group_keys": ["k-new"], "selected": True})
    with results_mod._selected_lock:
        assert "k-0" in results_mod._selected  # protected by the re-select
        assert "k-1" not in results_mod._selected  # evicted instead


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
