"""Comprehensive tests for backend/matching.py - MatchingEngine and fuzzy cache utilities.

Tests cover:
- Movie matching (IMDb ID, fuzzy title, year tolerance, missing search_key)
- TV season matching (IMDb+season, fuzzy title+season)
- No-match scenarios (unknown title, wrong year, empty index)
- Movie upgrade status (1080p->4K, DV, size, strict resolution, in-library)
- TV upgrade status (resolution, DV, size, in-library)
- Download history check
- Resolution preference skip
- Fuzzy cache functions
- Edge cases
"""

import pytest
from backend.matching import (
    MatchingEngine,
    cached_fuzz_ratio,
    cached_token_sort_ratio,
    clear_fuzzy_cache,
    get_fuzzy_cache_info,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_web(
    title="Test Movie",
    year=2020,
    res="1080p",
    size="10 GB",
    dovi=False,
    hdr="",
    url="https://example.com/movie",
    imdb_id=None,
    is_tv=False,
    season=None,
    episodes=None,
    search_key=None,
    episode_number=None,
):
    """Build a minimal web item dict for testing."""
    web = {
        "display_title": title,
        "year": year,
        "res": res,
        "size": size,
        "dovi": dovi,
        "hdr": hdr,
        "url": url,
        "imdb_id": imdb_id,
        "is_tv": is_tv,
        "season": season,
        "episodes": episodes,
    }
    if search_key is not None:
        web["search_key"] = search_key
    if episode_number is not None:
        web["episode_number"] = episode_number
    return web


def _make_plex(
    title="test movie",
    original="Test Movie",
    year=2020,
    res="1080p",
    size=10.0,
    dovi=False,
    hdr=False,
    imdb_id=None,
    rating_key="9999",
    season=None,
    episode_count=None,
):
    """Build a minimal Plex item dict for testing."""
    item = {
        "clean_title": title,
        "original_title": original,
        "year": year,
        "res": res,
        "size": size,
        "dovi": dovi,
        "hdr": hdr,
        "imdb_id": imdb_id,
        "rating_key": rating_key,
    }
    if season is not None:
        item["season"] = season
    if episode_count is not None:
        item["episode_count"] = episode_count
    return item


def _build_index(items):
    """Build a plex_index from a list of Plex items."""
    by_imdb = {}
    by_title = {}
    for item in items:
        imdb = item.get("imdb_id")
        if imdb:
            by_imdb.setdefault(imdb, []).append(item)
        title = item.get("clean_title", "").lower()
        if title:
            by_title.setdefault(title, []).append(item)
    return {"by_imdb": by_imdb, "by_title": by_title, "all_items": list(items)}


# ---------------------------------------------------------------------------
# Fuzzy cache module-level functions
# ---------------------------------------------------------------------------

class TestFuzzyCache:
    """Tests for cached_fuzz_ratio, cached_token_sort_ratio, clear/get cache."""

    def setup_method(self):
        clear_fuzzy_cache()

    def test_cached_fuzz_ratio_returns_int(self):
        score = cached_fuzz_ratio("hello", "hello")
        assert isinstance(score, int)
        assert score == 100

    def test_cached_fuzz_ratio_partial(self):
        score = cached_fuzz_ratio("the matrix", "matrix")
        assert 0 < score < 100

    def test_cached_token_sort_ratio_returns_int(self):
        score = cached_token_sort_ratio("dark knight the", "the dark knight")
        assert isinstance(score, int)
        assert score == 100

    def test_cache_info_structure(self):
        clear_fuzzy_cache()
        # Generate a miss then a hit
        cached_fuzz_ratio("a", "b")
        cached_fuzz_ratio("a", "b")
        info = get_fuzzy_cache_info()
        assert "ratio_hits" in info
        assert "ratio_misses" in info
        assert "ratio_size" in info
        assert "token_hits" in info
        assert "token_misses" in info
        assert "token_size" in info
        assert "total_hits" in info
        assert "total_misses" in info
        assert "hit_rate" in info

    def test_cache_hit_rate_after_repeated_calls(self):
        clear_fuzzy_cache()
        for _ in range(5):
            cached_fuzz_ratio("inception", "inception")
        info = get_fuzzy_cache_info()
        assert info["ratio_hits"] == 4   # first call is a miss
        assert info["ratio_misses"] == 1
        assert info["hit_rate"] > 0

    def test_clear_cache_resets(self):
        cached_fuzz_ratio("x", "y")
        clear_fuzzy_cache()
        info = get_fuzzy_cache_info()
        assert info["ratio_size"] == 0
        assert info["token_size"] == 0


# ---------------------------------------------------------------------------
# Movie matching
# ---------------------------------------------------------------------------

class TestFindMovieMatches:
    """Tests for MatchingEngine.find_movie_matches."""

    def test_match_by_imdb_id(self, matching_engine, plex_index):
        """Exact IMDb match should return the Plex item and is_uncertain=False."""
        web = _make_web(title="The Matrix", year=1999, imdb_id="tt0133093")
        matches, uncertain = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) == 1
        assert matches[0]["rating_key"] == "1001"
        assert uncertain is False

    def test_match_by_imdb_returns_all_copies(self, matching_engine):
        """When multiple Plex copies share an IMDb, all should be returned."""
        p1 = _make_plex(imdb_id="tt1111111", res="1080p", rating_key="A")
        p2 = _make_plex(imdb_id="tt1111111", res="4K", rating_key="B")
        idx = _build_index([p1, p2])
        web = _make_web(imdb_id="tt1111111", year=2020)
        matches, uncertain = matching_engine.find_movie_matches(web, idx)
        assert len(matches) == 2
        assert uncertain is False

    def test_fuzzy_match_by_title(self, matching_engine, plex_index):
        """If no IMDb, fall back to fuzzy title matching."""
        web = _make_web(
            title="The Matrix", year=1999,
            search_key="the matrix",
        )
        matches, uncertain = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) >= 1
        assert matches[0]["clean_title"] == "the matrix"

    def test_fuzzy_match_is_uncertain_without_imdb(self, matching_engine):
        """Fuzzy-only matches without IMDb should be flagged uncertain.

        Use a slightly different search_key than clean_title so the exact
        title hash lookup misses and the code falls through to fuzzy scan.
        """
        plex = _make_plex(title="some unique title here", original="Some Unique Title Here", year=2021)
        idx = _build_index([plex])
        web = _make_web(
            title="Some Unique Title Here!", year=2021,
            # search_key differs slightly from clean_title so hash lookup misses
            search_key="some unique title here!",
        )
        matches, uncertain = matching_engine.find_movie_matches(web, idx)
        assert len(matches) >= 1
        # Fuzzy match without imdb_id => uncertain
        assert uncertain is True

    def test_year_tolerance_within(self, matching_engine):
        """Movie year off by 1 (within default tolerance) should match."""
        plex = _make_plex(title="blade runner", original="Blade Runner 2049", year=2017)
        idx = _build_index([plex])
        # Web has year 2018, tolerance=1 => should match via exact title lookup
        web = _make_web(title="Blade Runner 2049", year=2018, search_key="blade runner")
        matches, _ = matching_engine.find_movie_matches(web, idx)
        assert len(matches) >= 1

    def test_year_tolerance_outside(self, matching_engine):
        """Movie year off by more than tolerance should NOT match."""
        plex = _make_plex(title="blade runner", original="Blade Runner", year=1982)
        idx = _build_index([plex])
        web = _make_web(title="Blade Runner", year=2020, search_key="blade runner")
        matches, _ = matching_engine.find_movie_matches(web, idx)
        assert len(matches) == 0

    def test_no_match_unknown_title(self, matching_engine, plex_index):
        """Completely unknown title returns no matches."""
        web = _make_web(title="Zzzzz NonExistent Movie Xyz", year=2023, search_key="zzzzz nonexistent movie xyz")
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) == 0

    def test_no_match_empty_index(self, matching_engine):
        """Empty Plex index should return no matches."""
        idx = _build_index([])
        web = _make_web(title="Anything", year=2020, search_key="anything")
        matches, _ = matching_engine.find_movie_matches(web, idx)
        assert len(matches) == 0

    def test_search_key_auto_generated(self, matching_engine, plex_index):
        """When search_key is absent, find_movie_matches should generate it."""
        web = _make_web(title="The Matrix", year=1999, imdb_id="tt0133093")
        # Ensure no search_key
        assert "search_key" not in web
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        # search_key should have been added
        assert "search_key" in web
        assert len(matches) >= 1

    def test_no_year_skips_fuzzy(self, matching_engine, plex_index):
        """If web year is 0 and no IMDb match, fuzzy matching is skipped."""
        web = _make_web(title="The Matrix", year=0, search_key="the matrix")
        matches, _ = matching_engine.find_movie_matches(web, plex_index)
        assert len(matches) == 0

    def test_imdb_takes_priority_over_fuzzy(self, matching_engine):
        """IMDb match should be used even when fuzzy would also match."""
        plex_right = _make_plex(title="dune", original="Dune", year=2021, imdb_id="tt1160419", rating_key="R1")
        plex_wrong = _make_plex(title="dune", original="Dune", year=1984, imdb_id="tt0087182", rating_key="R2")
        idx = _build_index([plex_right, plex_wrong])
        web = _make_web(title="Dune", year=2021, imdb_id="tt1160419", search_key="dune")
        matches, uncertain = matching_engine.find_movie_matches(web, idx)
        # Should only return the IMDb match, not fall through to fuzzy
        assert all(m["imdb_id"] == "tt1160419" for m in matches)
        assert uncertain is False


# ---------------------------------------------------------------------------
# TV season matching
# ---------------------------------------------------------------------------

class TestFindTVSeasonMatches:
    """Tests for MatchingEngine.find_tv_season_matches."""

    def test_match_by_imdb_and_season(self, matching_engine, plex_index):
        """IMDb + season combo should return exact TV match."""
        web = _make_web(
            title="Breaking Bad S01", year=2008, imdb_id="tt0903747",
            is_tv=True, season=1,
        )
        matches, uncertain = matching_engine.find_tv_season_matches(web, plex_index)
        assert len(matches) == 1
        assert matches[0]["season"] == 1
        assert uncertain is False

    def test_imdb_match_wrong_season_returns_empty(self, matching_engine, plex_index):
        """IMDb matches but wrong season should return empty."""
        web = _make_web(
            title="Breaking Bad S05", year=2008, imdb_id="tt0903747",
            is_tv=True, season=5,
        )
        matches, _ = matching_engine.find_tv_season_matches(web, plex_index)
        assert len(matches) == 0

    def test_fuzzy_match_tv_season(self, matching_engine, plex_index):
        """Fuzzy title + season should find a match when no IMDb ID."""
        web = _make_web(
            title="Breaking Bad", year=2008,
            is_tv=True, season=2,
            search_key="breaking bad",
        )
        matches, _ = matching_engine.find_tv_season_matches(web, plex_index)
        assert len(matches) >= 1
        assert all(m["season"] == 2 for m in matches)

    def test_tv_no_match_wrong_title(self, matching_engine, plex_index):
        """Unknown TV show returns no matches."""
        web = _make_web(
            title="Nonexistent Show XYZ", year=2023,
            is_tv=True, season=1,
            search_key="nonexistent show xyz",
        )
        matches, _ = matching_engine.find_tv_season_matches(web, plex_index)
        assert len(matches) == 0

    def test_tv_search_key_auto_generated(self, matching_engine, plex_index):
        """search_key should be auto-generated for TV items when missing."""
        web = _make_web(
            title="Breaking Bad", year=2008, imdb_id="tt0903747",
            is_tv=True, season=1,
        )
        assert "search_key" not in web
        matches, _ = matching_engine.find_tv_season_matches(web, plex_index)
        assert "search_key" in web
        assert len(matches) == 1

    def test_tv_debug_mode_logs(self, matching_engine, mock_app, plex_index):
        """Debug mode should produce log output."""
        mock_app.config["debug_mode"] = True
        web = _make_web(
            title="Breaking Bad", year=2008, imdb_id="tt0903747",
            is_tv=True, season=1,
        )
        matching_engine.find_tv_season_matches(web, plex_index)
        assert any("[DEBUG]" in log for log in mock_app._logs)


# ---------------------------------------------------------------------------
# Movie upgrade status
# ---------------------------------------------------------------------------

class TestCalculateMovieUpgradeStatus:
    """Tests for MatchingEngine.calculate_movie_upgrade_status."""

    def test_in_library_no_upgrade(self, matching_engine):
        """Same resolution, same size, no DV difference => In Library."""
        plex = _make_plex(res="1080p", size=15.0, dovi=False, rating_key="P1")
        web = _make_web(res="1080p", size="15 GB", dovi=False)
        status, color, info, pid = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY
        assert color == matching_engine.app.COLOR_IN_LIBRARY
        assert pid == "P1"

    def test_upgrade_1080_to_4k(self, matching_engine):
        """1080p Plex item + 4K web item => UPGRADE (4K)."""
        plex = _make_plex(res="1080p", size=15.0, rating_key="P1")
        web = _make_web(res="4K", size="60 GB")
        status, color, info, pid = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_4K
        assert color == matching_engine.app.COLOR_UPGRADE

    def test_upgrade_1080_to_4k_size_gate_enabled(self, matching_engine, mock_app):
        """With rule_1080_4k_size, 4K must also be bigger than local to upgrade.

        When plex has 1080p and web is 4K, same_res is empty, so the
        exact-match path handles the size gate in calculate_movie_exact_match_status
        only when the exact-match exists (same res).  For cross-resolution, the
        fallback path is used.  In fallback, 'Prefer 4K' + missing 4K => UPGRADE (4K)
        regardless of size gate.  To properly test the size gate we need BOTH a
        1080p and a 4K Plex copy so that exact is found (4K same_res exists).
        """
        mock_app.config["rule_1080_4k_size"] = True
        p_1080 = _make_plex(res="1080p", size=15.0, rating_key="P1")
        p_4k = _make_plex(res="4K", size=50.0, rating_key="P2")
        # Web is 4K but smaller than local 4K => in library (exact match handles this)
        web_small = _make_web(res="4K", size="45 GB")
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web_small, [p_1080, p_4k])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

        # Web is 4K and larger than local 4K => size upgrade
        web_big = _make_web(res="4K", size="100 GB")
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web_big, [p_1080, p_4k])
        assert "UPGRADE" in status
        assert color == matching_engine.app.COLOR_UPGRADE

    def test_upgrade_dv(self, matching_engine):
        """Plex has non-DV, web has DV same res => DV upgrade."""
        plex = _make_plex(res="4K", size=50.0, dovi=False, rating_key="P1")
        web = _make_web(res="4K", size="55 GB", dovi=True)
        status, color, info, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_DV_UPGRADE
        assert color == matching_engine.app.COLOR_DV_UPGRADE

    def test_no_dv_upgrade_when_already_dv(self, matching_engine):
        """If Plex already has DV, web DV should not trigger DV upgrade."""
        plex = _make_plex(res="4K", size=50.0, dovi=True, rating_key="P1")
        web = _make_web(res="4K", size="50 GB", dovi=True)
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_upgrade_size_1080p(self, matching_engine):
        """Same 1080p resolution, web significantly larger => size upgrade."""
        plex = _make_plex(res="1080p", size=10.0, dovi=False, rating_key="P1")
        # 20 GB >> 10 GB * 1.02 => upgrade
        web = _make_web(res="1080p", size="20 GB", dovi=False)
        status, color, info, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE
        assert color == matching_engine.app.COLOR_UPGRADE

    def test_upgrade_size_4k(self, matching_engine):
        """Same 4K resolution, web significantly larger => size upgrade."""
        plex = _make_plex(res="4K", size=40.0, dovi=False, rating_key="P1")
        web = _make_web(res="4K", size="80 GB", dovi=False)
        status, color, info, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE
        assert color == matching_engine.app.COLOR_UPGRADE

    def test_no_size_upgrade_when_marginal(self, matching_engine):
        """Size slightly larger (within sensitivity) => no upgrade."""
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        # 10.1 GB vs 10 GB at 2% sensitivity = threshold 10.2 => no upgrade
        web = _make_web(res="1080p", size="10.1 GB")
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_size_plus_dv_upgrade_1080p(self, matching_engine):
        """Bigger size + DV at 1080p => DV upgrade (Case 1 fires before Case 3).

        In the upgrade rule chain, Case 1 (DV upgrade) is checked before
        Case 3 (1080p size upgrade).  When web has DV and plex does not,
        Case 1 catches it first, yielding STATUS_DV_UPGRADE.
        """
        plex = _make_plex(res="1080p", size=10.0, dovi=False, rating_key="P1")
        web = _make_web(res="1080p", size="20 GB", dovi=True)
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_DV_UPGRADE
        assert color == matching_engine.app.COLOR_DV_UPGRADE

    def test_dv_loss_below_threshold_stays_in_library(self, matching_engine):
        """A bigger same-res file that would DROP Dolby Vision is NOT an upgrade
        unless it clears the higher DV-loss threshold (default 20%). +15% < 20%
        => In Library (this is the Killing Faith case)."""
        plex = _make_plex(res="4K", size=11.95, dovi=True, rating_key="P1")
        web = _make_web(res="4K", size="13.8 GB", dovi=False)   # +15%, no DV
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_dv_loss_above_threshold_is_size_upgrade(self, matching_engine):
        """Past the DV-loss threshold a bigger non-DV file still counts. +30% >= 20%."""
        plex = _make_plex(res="4K", size=10.0, dovi=True, rating_key="P1")
        web = _make_web(res="4K", size="13 GB", dovi=False)     # +30%, no DV
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE

    def test_dv_preserved_uses_normal_sensitivity(self, matching_engine):
        """When no DV is lost (plex has none), a small +15% still upgrades at the
        normal 2% sensitivity — the higher DV-loss threshold does not apply."""
        plex = _make_plex(res="4K", size=10.0, dovi=False, rating_key="P1")
        web = _make_web(res="4K", size="11.5 GB", dovi=False)   # +15%, DV on neither
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE

    def test_dv_loss_threshold_is_configurable(self, matching_engine, mock_app):
        """Lowering the DV-loss threshold re-enables the +15% DV-dropping upgrade."""
        mock_app.config["upgrade_dv_loss_sensitivity"] = 10   # 10% bar
        plex = _make_plex(res="4K", size=11.95, dovi=True, rating_key="P1")
        web = _make_web(res="4K", size="13.8 GB", dovi=False)  # +15% > 10%
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE

    def test_strict_resolution_mismatch_forces_missing(self, matching_engine, mock_app):
        """Strict mode with 1080p local + 4K web => MISSING (not an upgrade)."""
        mock_app.config["strict_resolution"] = True
        plex = _make_plex(res="1080p", size=15.0, rating_key="P1")
        web = _make_web(res="4K", size="60 GB")
        status, color, info, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        # strict_resolution means cross-res is treated as MISSING
        assert status == matching_engine.app.STATUS_MISSING
        assert color == matching_engine.app.COLOR_MISSING

    def test_strict_resolution_same_res_proceeds(self, matching_engine, mock_app):
        """Strict mode with same resolution should still detect upgrades."""
        mock_app.config["strict_resolution"] = True
        plex = _make_plex(res="1080p", size=10.0, dovi=False, rating_key="P1")
        web = _make_web(res="1080p", size="20 GB", dovi=False)
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE

    def test_rule_dv_disabled(self, matching_engine, mock_app):
        """When rule_dv is off, DV differences should not trigger upgrade."""
        mock_app.config["rule_dv"] = False
        plex = _make_plex(res="4K", size=50.0, dovi=False, rating_key="P1")
        web = _make_web(res="4K", size="50 GB", dovi=True)
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_rule_1080_4k_disabled(self, matching_engine, mock_app):
        """When rule_1080_4k is off and pref=Prefer 4K, 1080p->4K yields MISSING.

        The fallback path checks: pref='Prefer 4K', web='4K', 4K not in
        res_list, 1080p IS in res_list, AND rule_1080_4k is False => MISSING.
        This prevents upgrading to 4K when the user disabled that rule.
        """
        mock_app.config["rule_1080_4k"] = False
        plex = _make_plex(res="1080p", size=15.0, rating_key="P1")
        web = _make_web(res="4K", size="60 GB")
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_MISSING
        assert color == matching_engine.app.COLOR_MISSING

    def test_rule_1080_1080_disabled(self, matching_engine, mock_app):
        """When rule_1080_1080 is off, 1080p size upgrades are not flagged."""
        mock_app.config["rule_1080_1080"] = False
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        web = _make_web(res="1080p", size="50 GB")
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_rule_4k_4k_disabled(self, matching_engine, mock_app):
        """When rule_4k_4k is off, 4K size upgrades are not flagged."""
        mock_app.config["rule_4k_4k"] = False
        plex = _make_plex(res="4K", size=40.0, dovi=False, rating_key="P1")
        web = _make_web(res="4K", size="80 GB", dovi=False)
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_fallback_status_prefer_4k(self, matching_engine):
        """Fallback: Plex has 1080p, web is 4K, pref=Prefer 4K => UPGRADE (4K)."""
        plex = _make_plex(res="1080p", size=15.0, rating_key="P1")
        web = _make_web(res="4K", size="60 GB")
        # Force fallback by providing a plex item whose res != web_res
        # AND there is no same_res copy.  The single 1080p item matches only
        # through the fallback path when exact is None.
        # However: calculate_movie_upgrade_status finds same_res first.
        # To trigger fallback, we need web_res NOT in the matches' res set.
        # Since plex is 1080p and web is 4K, same_res will be empty => fallback.
        # Actually with default config rule_1080_4k=True, the exact match branch handles this.
        # Let's verify: exact is None (no same_res), so fallback is called.
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_4K

    def test_fallback_no_preference_size_upgrade(self, matching_engine, mock_app):
        """Fallback with pref=No Preference and bigger web => size upgrade."""
        mock_app.config["pref_res"] = "No Preference"
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        web = _make_web(res="4K", size="60 GB")
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE
        assert color == matching_engine.app.COLOR_UPGRADE

    def test_fallback_no_preference_no_upgrade(self, matching_engine, mock_app):
        """Fallback with pref=No Preference and smaller web => in library."""
        mock_app.config["pref_res"] = "No Preference"
        plex = _make_plex(res="1080p", size=80.0, rating_key="P1")
        web = _make_web(res="4K", size="10 GB")
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_multiple_copies_returns_highest_res_key(self, matching_engine):
        """plex_item_id should point to the highest resolution copy."""
        p1080 = _make_plex(res="1080p", size=15.0, rating_key="low")
        p4k = _make_plex(res="4K", size=55.0, dovi=True, rating_key="high")
        web = _make_web(res="4K", size="55 GB", dovi=True)
        _, _, _, pid = matching_engine.calculate_movie_upgrade_status(web, [p1080, p4k])
        assert pid == "high"


# ---------------------------------------------------------------------------
# TV upgrade status
# ---------------------------------------------------------------------------

class TestCalculateTVUpgradeStatus:
    """Tests for MatchingEngine.calculate_tv_upgrade_status."""

    def test_tv_in_library_no_upgrade(self, matching_engine):
        """Same res and smaller web => in library (check mark)."""
        match = _make_plex(
            res="1080p", size=45.0, season=1, episode_count=7, rating_key="T1"
        )
        web = _make_web(
            res="1080p", size="40 GB", is_tv=True, season=1, episodes=7,
        )
        status, color, info, is_upgrade = matching_engine.calculate_tv_upgrade_status(web, match)
        assert status == matching_engine.app.STATUS_IN_LIBRARY_CHECK
        assert color == matching_engine.app.COLOR_IN_LIBRARY
        assert is_upgrade is False

    def test_tv_resolution_upgrade_to_4k(self, matching_engine):
        """TV 1080p -> 4K => resolution upgrade."""
        match = _make_plex(res="1080p", size=45.0, season=1, episode_count=7)
        web = _make_web(res="4K", size="100 GB", is_tv=True, season=1, episodes=7)
        status, color, info, is_upgrade = matching_engine.calculate_tv_upgrade_status(web, match)
        assert status == matching_engine.app.STATUS_UPGRADE_4K
        assert color == matching_engine.app.COLOR_UPGRADE
        assert is_upgrade is True

    def test_tv_resolution_upgrade_1080p(self, matching_engine):
        """TV 720p -> 1080p => resolution upgrade with label."""
        match = _make_plex(res="720p", size=20.0, season=1, episode_count=10)
        web = _make_web(res="1080p", size="50 GB", is_tv=True, season=1, episodes=10)
        status, color, info, is_upgrade = matching_engine.calculate_tv_upgrade_status(web, match)
        assert "UPGRADE" in status
        assert is_upgrade is True

    def test_tv_dv_upgrade(self, matching_engine):
        """TV Plex non-DV + web DV same res => DV upgrade."""
        match = _make_plex(res="4K", size=80.0, dovi=False, season=1, episode_count=10)
        web = _make_web(res="4K", size="85 GB", dovi=True, is_tv=True, season=1, episodes=10)
        status, color, info, is_upgrade = matching_engine.calculate_tv_upgrade_status(web, match)
        assert status == matching_engine.app.STATUS_DV_UPGRADE
        assert color == matching_engine.app.COLOR_DV_UPGRADE
        assert is_upgrade is True

    def test_tv_size_upgrade(self, matching_engine):
        """TV same res, significantly bigger web => size upgrade."""
        match = _make_plex(res="1080p", size=30.0, season=1, episode_count=10)
        web = _make_web(res="1080p", size="60 GB", is_tv=True, season=1, episodes=10)
        status, color, info, is_upgrade = matching_engine.calculate_tv_upgrade_status(web, match)
        assert "UPGRADE" in status
        assert is_upgrade is True
        assert color == matching_engine.app.COLOR_UPGRADE

    def test_tv_episode_count_mismatch_warning(self, matching_engine):
        """Episode count mismatch should include a warning in info."""
        match = _make_plex(res="1080p", size=30.0, season=1, episode_count=10)
        web = _make_web(res="1080p", size="25 GB", is_tv=True, season=1, episodes=8)
        _, _, info, _ = matching_engine.calculate_tv_upgrade_status(web, match)
        assert "!" in info  # EMOJI_WARNING
        assert "10ep vs 8ep" in info

    def test_tv_single_episode_vs_season_pack(self, matching_engine):
        """Single episode against a season pack should show season pack info."""
        match = _make_plex(res="1080p", size=45.0, season=1, episode_count=7)
        web = _make_web(
            res="1080p", size="5 GB", is_tv=True, season=1,
            episode_number=3,
        )
        _, _, info, _ = matching_engine.calculate_tv_upgrade_status(web, match)
        assert "Season Pack" in info


# ---------------------------------------------------------------------------
# Download history
# ---------------------------------------------------------------------------

class TestCheckDownloadHistory:
    """Tests for MatchingEngine.check_download_history."""

    def test_not_in_history(self, matching_engine):
        web = _make_web(url="https://example.com/new-movie")
        assert matching_engine.check_download_history(web) is False

    def test_in_history(self, matching_engine, mock_app):
        url = "https://example.com/already-downloaded"
        mock_app.download_history.add(url)
        web = _make_web(url=url)
        assert matching_engine.check_download_history(web) is True

    def test_different_url_not_in_history(self, matching_engine, mock_app):
        mock_app.download_history.add("https://example.com/other")
        web = _make_web(url="https://example.com/different")
        assert matching_engine.check_download_history(web) is False


# ---------------------------------------------------------------------------
# Resolution preference skip
# ---------------------------------------------------------------------------

class TestShouldSkipByPreference:
    """Tests for MatchingEngine.should_skip_by_preference."""

    def test_prefer_1080p_skips_4k(self, matching_engine, mock_app):
        mock_app.config["pref_res"] = "Prefer 1080p"
        web = _make_web(res="4K")
        assert matching_engine.should_skip_by_preference(web) is True

    def test_prefer_1080p_does_not_skip_1080p(self, matching_engine, mock_app):
        mock_app.config["pref_res"] = "Prefer 1080p"
        web = _make_web(res="1080p")
        assert matching_engine.should_skip_by_preference(web) is False

    def test_prefer_4k_does_not_skip_4k(self, matching_engine, mock_app):
        mock_app.config["pref_res"] = "Prefer 4K"
        web = _make_web(res="4K")
        assert matching_engine.should_skip_by_preference(web) is False

    def test_prefer_4k_does_not_skip_1080p(self, matching_engine, mock_app):
        mock_app.config["pref_res"] = "Prefer 4K"
        web = _make_web(res="1080p")
        assert matching_engine.should_skip_by_preference(web) is False

    def test_no_preference_does_not_skip(self, matching_engine, mock_app):
        mock_app.config["pref_res"] = "No Preference"
        web = _make_web(res="4K")
        assert matching_engine.should_skip_by_preference(web) is False


# ---------------------------------------------------------------------------
# Codec/HDR preference
# ---------------------------------------------------------------------------

class TestCheckCodecPreference:
    """Tests for MatchingEngine.check_codec_preference."""

    def test_hevc_preference_x265(self, matching_engine, mock_app):
        mock_app.config["pref_hevc"] = True
        web = _make_web(title="Movie.2024.1080p.BluRay.x265-GROUP")
        is_match, pref_type = matching_engine.check_codec_preference(web)
        assert is_match is True
        assert pref_type == "HEVC"

    def test_hevc_preference_hevc(self, matching_engine, mock_app):
        mock_app.config["pref_hevc"] = True
        web = _make_web(title="Movie.2024.1080p.BluRay.HEVC-GROUP")
        is_match, pref_type = matching_engine.check_codec_preference(web)
        assert is_match is True
        assert pref_type == "HEVC"

    def test_hdr10plus_preference(self, matching_engine, mock_app):
        mock_app.config["pref_hdr10plus"] = True
        web = _make_web(title="Movie.2024.2160p.UHD.BluRay.HDR10+-GROUP")
        is_match, pref_type = matching_engine.check_codec_preference(web)
        assert is_match is True
        assert pref_type == "HDR10+"

    def test_no_codec_preference(self, matching_engine, mock_app):
        # Neither pref enabled
        web = _make_web(title="Movie.2024.1080p.BluRay.x265-GROUP")
        is_match, pref_type = matching_engine.check_codec_preference(web)
        assert is_match is False
        assert pref_type == ""


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------

class TestLogMatchDebugInfo:
    """Tests for MatchingEngine.log_match_debug_info."""

    def test_no_logging_when_debug_off(self, matching_engine, mock_app):
        mock_app.config["debug_mode"] = False
        mock_app._logs.clear()
        plex = _make_plex()
        web = _make_web()
        matching_engine.log_match_debug_info(web, [plex], is_tv=False)
        assert len(mock_app._logs) == 0

    def test_logging_when_debug_on_with_matches(self, matching_engine, mock_app):
        mock_app.config["debug_mode"] = True
        mock_app._logs.clear()
        plex = _make_plex()
        web = _make_web()
        matching_engine.log_match_debug_info(web, [plex], is_tv=False)
        assert len(mock_app._logs) > 0
        assert any("Comparing" in log for log in mock_app._logs)
        assert any("Matches found" in log for log in mock_app._logs)

    def test_logging_when_debug_on_no_matches(self, matching_engine, mock_app):
        mock_app.config["debug_mode"] = True
        mock_app._logs.clear()
        web = _make_web()
        matching_engine.log_match_debug_info(web, [], is_tv=False)
        assert any("No matches found" in log for log in mock_app._logs)

    def test_logging_tv_includes_season(self, matching_engine, mock_app):
        mock_app.config["debug_mode"] = True
        mock_app._logs.clear()
        web = _make_web(is_tv=True, season=3)
        matching_engine.log_match_debug_info(web, [], is_tv=True)
        assert any("S3" in log for log in mock_app._logs)


# ---------------------------------------------------------------------------
# Edge cases and integration
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge-case and integration tests."""

    def test_web_size_zero_does_not_crash(self, matching_engine):
        """Zero-size web item should not cause division by zero."""
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        web = _make_web(res="1080p", size="0 GB")
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_plex_size_zero_does_not_crash(self, matching_engine):
        """Zero-size Plex item should not cause division by zero."""
        plex = _make_plex(res="1080p", size=0.0, rating_key="P1")
        web = _make_web(res="1080p", size="10 GB")
        # Should not raise even though plex_size == 0
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        # With size 0 in plex, web 10 GB is larger: 10 > 0 * 1.02 => upgrade
        assert "UPGRADE" in status

    def test_missing_web_res(self, matching_engine):
        """Web item with unknown resolution should not crash."""
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        web = _make_web(res="?", size="10 GB")
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        # ? resolution is lower in RESOLUTION_ORDER than 1080p, so no res upgrade
        # Different res so fallback path is used
        assert status is not None

    def test_tv_upgrade_no_episodes_zero(self, matching_engine):
        """TV match with 0 episode_count should not divide by zero."""
        match = _make_plex(res="1080p", size=0.0, season=1, episode_count=0)
        web = _make_web(res="1080p", size="50 GB", is_tv=True, season=1, episodes=10)
        # Should not raise
        status, _, info, _ = matching_engine.calculate_tv_upgrade_status(web, match)
        assert status is not None

    def test_high_sensitivity_suppresses_small_upgrade(self, matching_engine, mock_app):
        """High upgrade_sensitivity should suppress marginal size differences."""
        mock_app.config["upgrade_sensitivity"] = 50  # 50%
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        web = _make_web(res="1080p", size="14 GB")  # 40% bigger, under 50%
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_IN_LIBRARY

    def test_low_sensitivity_flags_small_upgrade(self, matching_engine, mock_app):
        """Low upgrade_sensitivity should flag small size differences."""
        mock_app.config["upgrade_sensitivity"] = 1  # 1%
        plex = _make_plex(res="1080p", size=10.0, rating_key="P1")
        web = _make_web(res="1080p", size="10.2 GB")  # 2% bigger, over 1%
        status, _, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE

    def test_movie_fallback_strict_resolution_missing(self, matching_engine, mock_app):
        """Fallback with strict_resolution and res mismatch => MISSING."""
        mock_app.config["strict_resolution"] = True
        mock_app.config["pref_res"] = "Prefer 4K"
        plex = _make_plex(res="1080p", size=15.0, rating_key="P1")
        web = _make_web(res="4K", size="60 GB")
        status, color, _, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_MISSING
        assert color == matching_engine.app.COLOR_MISSING

    def test_fuzzy_match_core_skips_very_different_length_titles(self, matching_engine):
        """Titles with length difference > TITLE_LENGTH_TOLERANCE should not match."""
        plex = _make_plex(
            title="ab", original="Ab", year=2020,
        )
        idx = _build_index([plex])
        web = _make_web(
            title="A Very Long Movie Title That Is Nothing Like Ab",
            year=2020,
            search_key="a very long movie title that is nothing like ab",
        )
        matches, _ = matching_engine.find_movie_matches(web, idx)
        assert len(matches) == 0

    def test_upgrade_sensitivity_percentage_calculation(self, matching_engine):
        """Verify the percentage shown in upgrade info is correct."""
        plex = _make_plex(res="1080p", size=10.0, dovi=False, rating_key="P1")
        web = _make_web(res="1080p", size="20 GB", dovi=False)
        status, _, info, _ = matching_engine.calculate_movie_upgrade_status(web, [plex])
        assert status == matching_engine.app.STATUS_UPGRADE_SIZE
        # 20 vs 10 = +100%
        assert "+100%" in info

    def test_tv_per_episode_size_comparison(self, matching_engine):
        """TV size upgrade uses per-episode comparison when episode counts available."""
        match = _make_plex(res="1080p", size=20.0, season=1, episode_count=10)
        # Plex: 20 GB / 10 eps = 2 GB/ep
        # Web: 50 GB / 10 eps = 5 GB/ep => +150%
        web = _make_web(res="1080p", size="50 GB", is_tv=True, season=1, episodes=10)
        status, _, info, is_upgrade = matching_engine.calculate_tv_upgrade_status(web, match)
        assert is_upgrade is True
        assert "+150%" in status
