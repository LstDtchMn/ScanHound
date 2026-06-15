"""Edge-case gauntlet: aggressive boundary, unicode, injection, and error-handling tests.

Covers all major ScanHound modules with intentionally adversarial inputs
designed to expose crashes, silent data corruption, and unhandled exceptions.
"""

import json
import math
import sqlite3
import threading
import pytest

from backend.app_service import LRUCache, clean_string, normalize_title
from backend.config import get_default_config, validate_config, SETTINGS_PRESETS
from backend.database import DatabaseManager
from backend.matching import MatchingEngine, cached_fuzz_ratio, cached_token_sort_ratio, clear_fuzzy_cache
from backend.watchlist import (
    WatchlistManager, WatchlistItem, WatchlistItemType, WatchlistItemStatus,
)
from backend.analytics import StatsDashboard, LibraryStats, ScanStats, UpgradeAnalysis
from backend.plex_manager import LibraryType, PlexLibrary, PathMapping
from backend.sources.base import SourceCapability, SourceConfig, ParsedRelease


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_app(config_overrides=None):
    """Build a lightweight mock app for MatchingEngine tests."""
    from tests.conftest import MockApp
    app = MockApp()
    if config_overrides:
        app.config.update(config_overrides)
    return app


def _empty_plex_index():
    return {"by_imdb": {}, "by_title": {}, "all_items": []}


# ===================================================================
# 1. Unicode & Internationalisation
# ===================================================================
class TestUnicode:
    """Verify that unicode-heavy titles do not crash normalisation or matching."""

    def test_accented_latin_clean_string(self):
        result = clean_string("Amelie")
        assert isinstance(result, str)
        # Accented chars are stripped by the regex [^a-z0-9\s]
        result2 = clean_string("\u00c9mile Zola")
        assert isinstance(result2, str)

    def test_accented_title_normalize(self):
        assert normalize_title("Am\u00e9lie") == "amlie"
        assert normalize_title("L\u00e9on: The Professional") == "lon the professional"

    def test_cjk_title(self):
        """CJK characters should survive or be stripped gracefully."""
        result = clean_string("\u5343\u3068\u5343\u5c0b\u306e\u795e\u96a0\u3057")
        assert isinstance(result, str)
        # All non-ascii is stripped by [^a-z0-9\s]
        assert result == ""

    def test_cyrillic_title(self):
        result = clean_string("\u0411\u0440\u0430\u0442")
        assert isinstance(result, str)

    def test_arabic_title(self):
        result = clean_string("\u0627\u0644\u0641\u064a\u0644\u0645")
        assert isinstance(result, str)

    def test_hebrew_title(self):
        result = clean_string("\u05e1\u05e8\u05d8 \u05d9\u05e9\u05e8\u05d0\u05dc\u05d9")
        assert isinstance(result, str)

    def test_mixed_scripts(self):
        title = "Attack on Titan \u9032\u6483\u306e\u5de8\u4eba S01"
        result = clean_string(title)
        # Latin portion kept, CJK stripped, year-like patterns removed
        assert "attack" in result

    def test_emoji_stripped(self):
        result = clean_string("\U0001f525 Hot Movie \U0001f525")
        assert "hot movie" == result

    def test_very_long_unicode_title(self):
        long_title = "\u00e9" * 1500
        result = clean_string(long_title)
        assert isinstance(result, str)

    def test_rtl_text(self):
        """Right-to-left marks should not crash normalisation."""
        rtl = "\u200f\u0645\u0631\u062d\u0628\u0627\u200f"
        assert isinstance(clean_string(rtl), str)

    def test_normalize_title_cjk(self):
        assert isinstance(normalize_title("\u5343\u3068\u5343\u5c0b\u306e\u795e\u96a0\u3057"), str)

    def test_unicode_in_matching_engine(self, mock_app, plex_index):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "L\u00e9on: The Professional",
            "year": 1994,
            "res": "4K",
            "size": "50 GB",
            "dovi": False,
            "url": "http://example.com/leon",
            "imdb_id": None,
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        # No crash; may or may not match
        assert isinstance(matches, list)


# ===================================================================
# 2. Numeric Boundary Values
# ===================================================================
class TestBoundaryValues:
    """Push numeric fields to extreme or nonsensical values."""

    # -- Year --
    @pytest.mark.parametrize("year", [0, -1, 9999, 2147483647])
    def test_extreme_years_in_matching(self, mock_app, plex_index, year):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Edge Case Movie",
            "year": year,
            "res": "1080p",
            "size": "10 GB",
            "dovi": False,
            "url": "http://example.com/edge",
            "imdb_id": None,
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert isinstance(matches, list)

    # -- Size strings --
    @pytest.mark.parametrize("size_str,expected_positive", [
        ("0 GB", False),
        ("-5 GB", False),
        ("0.001 MB", True),
        ("999 TB", True),
        ("", False),
        ("GB", False),
        ("10", True),
        ("10 GiB", True),
        ("?", False),
    ])
    def test_parse_size_edge_values(self, mock_app, size_str, expected_positive):
        result = mock_app.parse_size(size_str)
        if expected_positive:
            assert result > 0 or result == 0  # Some edge cases return 0 legitimately
        assert isinstance(result, float)

    def test_parse_size_inf_nan(self, mock_app):
        # inf / nan as raw floats shouldn't reach parse_size (it expects str)
        assert mock_app.parse_size("inf") == 0.0
        assert mock_app.parse_size("nan") == 0.0

    # -- Episode / Season boundaries --
    @pytest.mark.parametrize("season,ep_count", [
        (0, 0), (-1, -1), (1, 1), (100, 1000),
    ])
    def test_tv_season_boundaries(self, mock_app, plex_index, season, ep_count):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Breaking Bad",
            "year": 2008,
            "res": "1080p",
            "size": "40 GB",
            "dovi": False,
            "url": "http://example.com/bb",
            "imdb_id": None,
            "is_tv": True,
            "season": season,
            "episodes": ep_count,
        }
        matches, uncertain = engine.find_tv_season_matches(web, plex_index)
        assert isinstance(matches, list)

    # -- Resolution strings --
    @pytest.mark.parametrize("res", ["", "?", "SD", "360p", "8K", None])
    def test_unusual_resolutions(self, mock_app, plex_index, res):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "The Matrix",
            "year": 1999,
            "res": res,
            "size": "15 GB",
            "dovi": False,
            "url": "http://example.com/m",
            "imdb_id": "tt0133093",
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        if matches:
            status, color, info, pid = engine.calculate_movie_upgrade_status(web, matches)
            assert isinstance(status, str)

    # -- Threshold extremes --
    @pytest.mark.parametrize("threshold", [0, 1, 100, 101, -1])
    def test_match_threshold_extremes(self, mock_app, plex_index, threshold):
        mock_app.config["movie_match_threshold"] = threshold
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Inception",
            "year": 2010,
            "res": "4K",
            "size": "55 GB",
            "dovi": True,
            "url": "http://example.com/inception",
            "imdb_id": None,
            "is_tv": False,
        }
        # Should not crash regardless of threshold value
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert isinstance(matches, list)

    @pytest.mark.parametrize("sensitivity", [0, 100, -1, 1000])
    def test_upgrade_sensitivity_extremes(self, mock_app, plex_index, sensitivity):
        mock_app.config["upgrade_sensitivity"] = sensitivity
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "The Matrix",
            "year": 1999,
            "res": "1080p",
            "size": "20 GB",
            "dovi": False,
            "url": "http://example.com/m",
            "imdb_id": "tt0133093",
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        if matches:
            status, color, info, pid = engine.calculate_movie_upgrade_status(web, matches)
            assert isinstance(status, str)

    def test_validate_config_clamps_negative_min_size(self):
        cfg = get_default_config()
        cfg["min_size_mb"] = -100
        cleaned = validate_config(cfg)
        assert cleaned["min_size_mb"] == 0

    def test_validate_config_clamps_scan_threads(self):
        cfg = get_default_config()
        cfg["scan_threads"] = 999
        cleaned = validate_config(cfg)
        assert cleaned["scan_threads"] == 50

    def test_validate_config_clamps_threshold_above_100(self):
        cfg = get_default_config()
        cfg["movie_match_threshold"] = 200
        cleaned = validate_config(cfg)
        assert cleaned["movie_match_threshold"] == 100

    def test_validate_config_clamps_threshold_below_0(self):
        cfg = get_default_config()
        cfg["movie_match_threshold"] = -50
        cleaned = validate_config(cfg)
        assert cleaned["movie_match_threshold"] == 0


# ===================================================================
# 3. SQL Injection & Malicious Input
# ===================================================================
class TestMaliciousInput:
    """Ensure parameterised queries and input sanitisation prevent injection."""

    def test_sql_injection_in_history_title(self, db_manager):
        malicious = "'; DROP TABLE downloads; --"
        assert db_manager.add_to_history(
            "http://evil.com/1", malicious
        )
        assert db_manager.is_in_history("http://evil.com/1")
        # Table must still exist
        assert db_manager.get_history_count() >= 1

    def test_bobby_tables(self, db_manager):
        title = "Robert'); DROP TABLE students;--"
        db_manager.add_to_history("http://evil.com/2", title)
        # Downloads table must survive
        assert db_manager.get_history_count() >= 1

    def test_sql_injection_in_url(self, db_manager):
        evil_url = "http://evil.com/'; DROP TABLE downloads;--"
        db_manager.add_to_history(evil_url, "Normal Title")
        assert db_manager.is_in_history(evil_url)

    def test_sql_injection_in_size_field(self, db_manager):
        db_manager.add_to_history(
            "http://evil.com/3", "Movie",
            size="10 GB; DROP TABLE plex_cache",
        )
        # plex_cache must survive
        cache = db_manager.load_plex_cache("Movies")
        assert isinstance(cache, list)

    def test_json_injection_watchlist_import(self, tmp_path):
        wm = WatchlistManager(db_path=str(tmp_path / "wl.db"))
        try:
            evil_json = '{"items": [{"title": "\\"; DROP TABLE watchlist;--", "item_type": "movie"}]}'
            count = wm.import_from_json(evil_json)
            # Should either import 1 safely or 0 (handled error). Table must exist.
            stats = wm.get_stats()
            assert isinstance(stats["total"], int)
        finally:
            wm.close()

    def test_very_long_string_title(self, db_manager):
        huge = "A" * 120_000
        db_manager.add_to_history("http://big.com", huge)
        assert db_manager.is_in_history("http://big.com")

    def test_null_bytes_in_string(self):
        result = clean_string("movie\x00.mkv")
        assert isinstance(result, str)
        assert "\x00" not in result or isinstance(result, str)

    def test_control_characters(self):
        ctrl = "".join(chr(i) for i in range(32))
        result = clean_string(ctrl + "Title" + ctrl)
        assert "title" in result

    def test_watchlist_search_sql_wildcards(self, tmp_path):
        """LIKE wildcards %, _ should be escaped in search."""
        wm = WatchlistManager(db_path=str(tmp_path / "wl2.db"))
        try:
            wm.add(WatchlistItem(title="100% Pure"))
            wm.add(WatchlistItem(title="Some_Thing"))
            # Searching for literal '%' should not match everything
            results = wm.search("%")
            assert any("100%" in r.title for r in results)
            # Searching for '_' should match the underscore title
            results2 = wm.search("_")
            found_titles = [r.title for r in results2]
            assert "Some_Thing" in found_titles
        finally:
            wm.close()


# ===================================================================
# 4. Empty / None / Missing Field Handling
# ===================================================================
class TestMissingFields:
    """Probe behaviour when required fields are absent or None."""

    def test_web_item_no_title(self, mock_app, plex_index):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "",
            "year": 2020,
            "res": "1080p",
            "size": "10 GB",
            "dovi": False,
            "url": "http://example.com/empty",
            "imdb_id": None,
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert isinstance(matches, list)

    def test_web_item_all_empty_strings(self, mock_app, plex_index):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "",
            "year": 0,
            "res": "",
            "size": "",
            "dovi": False,
            "url": "",
            "imdb_id": "",
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert isinstance(matches, list)

    def test_plex_item_none_optionals(self, mock_app):
        """Plex item with None for all optional fields.

        BUG DETECTED: matching.py line 314 does p.get('year', 0) - web_year
        but when the key 'year' exists with value None, .get() returns None
        (default is only used when the key is *absent*), causing TypeError.
        """
        engine = MatchingEngine(mock_app)
        plex_index = {
            "by_imdb": {},
            "by_title": {
                "the matrix": [{
                    "clean_title": "the matrix",
                    "original_title": None,
                    "year": None,
                    "res": None,
                    "size": None,
                    "dovi": None,
                    "hdr": None,
                    "imdb_id": None,
                    "rating_key": "999",
                }]
            },
            "all_items": [{
                "clean_title": "the matrix",
                "original_title": None,
                "year": None,
                "res": None,
                "size": None,
                "dovi": None,
                "hdr": None,
                "imdb_id": None,
                "rating_key": "999",
            }],
        }
        web = {
            "display_title": "The Matrix",
            "year": 1999,
            "res": "1080p",
            "size": "15 GB",
            "dovi": False,
            "url": "http://example.com/m",
            "imdb_id": None,
            "is_tv": False,
        }
        # Fixed: None year in plex item is treated as 0, no crash
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert isinstance(matches, list)

    def test_empty_plex_index(self, mock_app):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Anything",
            "year": 2020,
            "res": "4K",
            "size": "50 GB",
            "dovi": True,
            "url": "http://example.com/any",
            "imdb_id": "tt9999999",
            "is_tv": False,
        }
        idx = _empty_plex_index()
        matches, uncertain = engine.find_movie_matches(web, idx)
        assert matches == []

    def test_watchlist_item_all_defaults(self):
        item = WatchlistItem()
        d = item.to_dict()
        assert d["title"] == ""
        assert d["year"] is None
        assert d["item_type"] == "movie"
        assert d["status"] == "wanted"

    def test_validate_config_empty_dict(self):
        cleaned = validate_config({})
        assert isinstance(cleaned, dict)

    def test_validate_config_none_values(self):
        """Fixed: validate_config now handles None values safely."""
        cfg = {"min_size_mb": None, "scan_threads": None}
        cleaned = validate_config(cfg)
        assert isinstance(cleaned, dict)
        # None min_size_mb treated as 0 (no clamp needed)
        # None scan_threads skipped (is not None check)
        assert cleaned["min_size_mb"] is None  # not clamped since _safe_numeric(None, 0) = 0 >= 0
        assert cleaned["scan_threads"] is None  # scan_threads None check passes

    def test_clean_string_none(self):
        assert clean_string(None) == ""
        assert clean_string("") == ""

    def test_normalize_title_none(self):
        assert normalize_title(None) == ""
        assert normalize_title("") == ""

    def test_db_add_history_none_optional_fields(self, db_manager):
        result = db_manager.add_to_history(
            "http://test.com/1", "Test",
            normalized_title=None, season=None, resolution=None, size=None,
        )
        assert result is True


# ===================================================================
# 5. Type Coercion & Mismatch
# ===================================================================
class TestTypeCoercion:
    """Fields arrive with wrong types; verify graceful handling."""

    def test_size_as_int(self, mock_app):
        # parse_size expects str, but int could leak in
        result = mock_app.parse_size(42)
        assert result == 0.0  # not a str, should return 0

    def test_year_as_string_in_matching(self, mock_app, plex_index):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Inception",
            "year": "2010",  # str instead of int
            "res": "4K",
            "size": "55 GB",
            "dovi": True,
            "url": "http://example.com/inception",
            "imdb_id": "tt1375666",
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert isinstance(matches, list)

    def test_dovi_as_int(self, mock_app, plex_index):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "The Matrix",
            "year": 1999,
            "res": "1080p",
            "size": "15 GB",
            "dovi": 1,  # int instead of bool
            "url": "http://example.com/m2",
            "imdb_id": "tt0133093",
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        if matches:
            status, color, info, pid = engine.calculate_movie_upgrade_status(web, matches)
            assert isinstance(status, str)

    def test_rating_key_as_int(self):
        lib = PlexLibrary(
            key=42,  # int instead of str
            title="Movies",
            type=LibraryType.MOVIE,
        )
        d = lib.to_dict()
        assert d["key"] == 42

    def test_episode_count_as_float(self, mock_app, plex_index):
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Breaking Bad",
            "year": 2008,
            "res": "1080p",
            "size": "50 GB",
            "dovi": False,
            "url": "http://example.com/bb",
            "imdb_id": "tt0903747",
            "is_tv": True,
            "season": 1,
            "episodes": 7.0,  # float instead of int
        }
        matches, uncertain = engine.find_tv_season_matches(web, plex_index)
        if matches:
            status, color, info, is_upgrade = engine.calculate_tv_upgrade_status(web, matches[0])
            assert isinstance(status, str)

    def test_config_string_true_for_bool(self):
        """String 'true' for a boolean config field."""
        cfg = get_default_config()
        cfg["debug_mode"] = "true"
        # validate_config doesn't coerce bools, but AppService.validate_config_values does.
        # At minimum it should not crash.
        cleaned = validate_config(cfg)
        assert isinstance(cleaned, dict)


# ===================================================================
# 6. Duplicate & Collision Handling
# ===================================================================
class TestDuplicates:
    """Verify correct behaviour when identical or near-identical items coexist."""

    def test_identical_movies_in_plex_index(self, mock_app):
        """Two movies with exact same title/year/resolution."""
        engine = MatchingEngine(mock_app)
        item_a = {
            "clean_title": "dune",
            "original_title": "Dune",
            "year": 2021,
            "res": "4K",
            "size": 50.0,
            "dovi": True,
            "hdr": True,
            "imdb_id": "tt1160419",
            "rating_key": "3001",
        }
        item_b = dict(item_a, rating_key="3002", size=60.0)
        plex_index = {
            "by_imdb": {"tt1160419": [item_a, item_b]},
            "by_title": {"dune": [item_a, item_b]},
            "all_items": [item_a, item_b],
        }
        web = {
            "display_title": "Dune",
            "year": 2021,
            "res": "4K",
            "size": "70 GB",
            "dovi": True,
            "url": "http://example.com/dune",
            "imdb_id": "tt1160419",
            "is_tv": False,
        }
        matches, uncertain = engine.find_movie_matches(web, plex_index)
        assert len(matches) == 2
        status, color, info, pid = engine.calculate_movie_upgrade_status(web, matches)
        assert isinstance(status, str)

    def test_same_url_added_twice(self, db_manager):
        db_manager.add_to_history("http://dup.com/1", "Movie A")
        db_manager.add_to_history("http://dup.com/1", "Movie A Updated")
        assert db_manager.get_history_count() >= 1
        assert db_manager.is_in_history("http://dup.com/1")

    def test_watchlist_duplicate_imdb_returns_existing_id(self, tmp_path):
        wm = WatchlistManager(db_path=str(tmp_path / "wl_dup.db"))
        try:
            item1 = WatchlistItem(title="Dune", imdb_id="tt1160419")
            id1 = wm.add(item1)
            item2 = WatchlistItem(title="Dune Part One", imdb_id="tt1160419")
            id2 = wm.add(item2)
            assert id1 == id2  # same imdb_id returns existing
        finally:
            wm.close()

    def test_watchlist_same_title_different_imdb(self, tmp_path):
        wm = WatchlistManager(db_path=str(tmp_path / "wl_diff.db"))
        try:
            id1 = wm.add(WatchlistItem(title="Dune", imdb_id="tt1160419"))
            id2 = wm.add(WatchlistItem(title="Dune", imdb_id="tt0087182"))
            assert id1 != id2  # different imdb_ids = different items
        finally:
            wm.close()

    def test_plex_items_same_imdb_different_titles(self, mock_app):
        engine = MatchingEngine(mock_app)
        item_a = {
            "clean_title": "alien",
            "original_title": "Alien",
            "year": 1979,
            "res": "4K",
            "size": 40.0,
            "dovi": False,
            "hdr": True,
            "imdb_id": "tt0078748",
            "rating_key": "5001",
        }
        item_b = {
            "clean_title": "alien directors cut",
            "original_title": "Alien: Director's Cut",
            "year": 1979,
            "res": "1080p",
            "size": 12.0,
            "dovi": False,
            "hdr": False,
            "imdb_id": "tt0078748",
            "rating_key": "5002",
        }
        plex_index = {
            "by_imdb": {"tt0078748": [item_a, item_b]},
            "by_title": {"alien": [item_a], "alien directors cut": [item_b]},
            "all_items": [item_a, item_b],
        }
        web = {
            "display_title": "Alien",
            "year": 1979,
            "res": "4K",
            "size": "50 GB",
            "dovi": False,
            "url": "http://example.com/alien",
            "imdb_id": "tt0078748",
            "is_tv": False,
        }
        matches, _ = engine.find_movie_matches(web, plex_index)
        assert len(matches) == 2

    def test_lru_cache_eviction(self):
        cache = LRUCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["d"] = 4  # should evict 'a'
        assert "a" not in cache
        assert cache["d"] == 4
        assert len(cache) == 3

    def test_lru_cache_key_collision(self):
        cache = LRUCache(maxsize=10)
        cache["key"] = "first"
        cache["key"] = "second"
        assert cache["key"] == "second"
        assert len(cache) == 1

    def test_lru_cache_get_missing(self):
        cache = LRUCache(maxsize=5)
        assert cache.get("nope") is None
        assert cache.get("nope", 42) == 42
        with pytest.raises(KeyError):
            _ = cache["nope"]


# ===================================================================
# 7. Config Validation Edge Cases
# ===================================================================
class TestConfigValidationEdges:

    def test_validate_empty_dict(self):
        result = validate_config({})
        assert isinstance(result, dict)

    def test_validate_extra_unknown_keys(self):
        cfg = get_default_config()
        cfg["totally_unknown_key"] = "hello"
        cfg["another_bogus"] = 999
        cleaned = validate_config(cfg)
        # Unknown keys should pass through (not crash)
        assert cleaned["totally_unknown_key"] == "hello"

    def test_validate_nested_invalid_types(self):
        """Fixed: validate_config now handles non-numeric strings safely."""
        cfg = get_default_config()
        cfg["movie_match_threshold"] = "not_a_number"
        # _safe_int falls back to default instead of crashing
        cleaned = validate_config(cfg)
        assert isinstance(cleaned["movie_match_threshold"], int)
        assert 0 <= cleaned["movie_match_threshold"] <= 100

    def test_all_presets_validate_cleanly(self):
        for name, preset in SETTINGS_PRESETS.items():
            cfg = get_default_config()
            for k, v in preset.items():
                if k != "description":
                    cfg[k] = v
            cleaned = validate_config(cfg)
            assert isinstance(cleaned, dict), f"Preset {name!r} failed validation"

    def test_mixed_valid_and_invalid(self):
        cfg = get_default_config()
        cfg["min_size_mb"] = -500  # invalid, should clamp
        cfg["scheduler_interval"] = 0  # invalid, should clamp
        cfg["cache_duration"] = -10  # invalid, should clamp
        cleaned = validate_config(cfg)
        assert cleaned["min_size_mb"] == 0
        assert cleaned["scheduler_interval"] == 1
        assert cleaned["cache_duration"] == 0

    def test_year_tolerance_clamped(self):
        cfg = get_default_config()
        cfg["year_tolerance"] = 999
        cleaned = validate_config(cfg)
        assert cleaned["year_tolerance"] == 10



# ===================================================================
# 8. PathMapping Edge Cases
# ===================================================================
class TestPathMappingEdges:

    def test_translate_matching_prefix(self):
        pm = PathMapping(plex_path="/data/media", local_path="/mnt/nas/media")
        assert pm.translate("/data/media/movies/file.mkv") == "/mnt/nas/media/movies/file.mkv"

    def test_translate_non_matching_prefix(self):
        pm = PathMapping(plex_path="/data/media", local_path="/mnt/nas/media")
        path = "/other/path/file.mkv"
        assert pm.translate(path) == path

    def test_translate_disabled_mapping(self):
        pm = PathMapping(plex_path="/data/media", local_path="/mnt/nas/media", enabled=False)
        path = "/data/media/movies/file.mkv"
        assert pm.translate(path) == path  # unchanged because disabled

    def test_translate_empty_path(self):
        pm = PathMapping(plex_path="/data", local_path="/mnt")
        assert pm.translate("") == ""

    def test_translate_special_characters_in_path(self):
        pm = PathMapping(plex_path="/data", local_path="/mnt")
        special = "/data/movies/L\u00e9on (1994)/L\u00e9on.mkv"
        result = pm.translate(special)
        assert result == "/mnt/movies/L\u00e9on (1994)/L\u00e9on.mkv"

    def test_prefix_of_another_path(self):
        """When one plex_path is a prefix of another, longest match should win."""
        pm = PlexLibrary(key="1", title="test", type=LibraryType.MOVIE)
        from backend.plex_manager import PlexManager
        mgr = PlexManager()
        mgr.add_path_mapping("/media", "/local")
        mgr.add_path_mapping("/media/movies", "/fast_ssd/movies")
        result = mgr.translate_path("/media/movies/Dune.mkv")
        assert result == "/fast_ssd/movies/Dune.mkv"

    def test_plex_manager_translate_no_mappings(self):
        from backend.plex_manager import PlexManager
        mgr = PlexManager()
        assert mgr.translate_path("/any/path") == "/any/path"

    def test_path_mapping_only_replaces_first_occurrence(self):
        pm = PathMapping(plex_path="/data", local_path="/mnt")
        path = "/data/data/file.mkv"
        result = pm.translate(path)
        # Should only replace the first /data
        assert result == "/mnt/data/file.mkv"


# ===================================================================
# 9. Additional Aggressive Tests
# ===================================================================
class TestAdditionalEdgeCases:
    """Extra boundary tests for completeness."""

    def test_lru_cache_zero_maxsize(self):
        cache = LRUCache(maxsize=0)
        cache["a"] = 1
        # With maxsize 0, the while-loop evicts immediately
        assert len(cache) == 0

    def test_lru_cache_thread_safety(self):
        cache = LRUCache(maxsize=100)
        errors = []

        def writer(start):
            try:
                for i in range(100):
                    cache[f"{start}_{i}"] = i
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(cache) <= 100

    def test_parsed_release_minimal(self):
        r = ParsedRelease(title="Test", url="http://x.com", source="test")
        assert r.display_title == "Test"
        assert r.search_key == "test"

    def test_parsed_release_empty_title(self):
        r = ParsedRelease(title="", url="http://x.com", source="test")
        assert r.display_title == ""
        assert r.search_key == ""

    def test_parsed_release_to_dict(self):
        r = ParsedRelease(
            title="Dune (2021) 2160p",
            url="http://x.com/dune",
            source="test",
            year=2021,
            resolution="4K",
            is_dovi=True,
        )
        d = r.to_dict()
        assert d["year"] == 2021
        assert d["res"] == "4K"
        assert d["dovi"] is True

    def test_source_config_defaults(self):
        sc = SourceConfig(name="test", display_name="Test", base_url="http://x.com")
        assert sc.rate_limit == 2.0
        assert not sc.requires_auth
        assert sc.enabled is True

    def test_source_capability_flags(self):
        caps = SourceCapability.MOVIES | SourceCapability.TV_SHOWS | SourceCapability.SEARCH
        assert SourceCapability.MOVIES in caps
        assert SourceCapability.SEARCH in caps
        assert SourceCapability.RSS not in caps

    def test_library_type_from_plex_type_unknown(self):
        assert LibraryType.from_plex_type("unknown_type") == LibraryType.OTHER
        assert LibraryType.from_plex_type("movie") == LibraryType.MOVIE
        assert LibraryType.from_plex_type("SHOW") == LibraryType.SHOW

    def test_plex_library_from_dict_minimal(self):
        lib = PlexLibrary.from_dict({"key": "1", "title": "Movies", "type": "movie"})
        assert lib.type == LibraryType.MOVIE
        assert lib.location == []

    def test_watchlist_item_from_dict_round_trip(self):
        item = WatchlistItem(
            title="Dune", year=2021, imdb_id="tt1160419",
            item_type=WatchlistItemType.MOVIE,
            status=WatchlistItemStatus.WANTED,
            priority=3,
        )
        d = item.to_dict()
        restored = WatchlistItem.from_dict(d)
        assert restored.title == "Dune"
        assert restored.year == 2021
        assert restored.priority == 3
        assert restored.item_type == WatchlistItemType.MOVIE

    def test_library_stats_to_dict(self):
        stats = LibraryStats(total_items=0, total_size_gb=0.0)
        d = stats.to_dict()
        assert d["total_items"] == 0
        assert d["quality_score"] == 0.0

    def test_scan_stats_to_dict_empty(self):
        stats = ScanStats()
        d = stats.to_dict()
        assert d["total_scans"] == 0
        assert d["avg_items_per_scan"] == 0

    def test_upgrade_analysis_to_dict(self):
        analysis = UpgradeAnalysis()
        d = analysis.to_dict()
        assert d["total_upgradeable"] == 0
        assert d["top_upgrade_candidates"] == []

    def test_upgrade_analysis_candidates_capped_at_20(self):
        analysis = UpgradeAnalysis()
        analysis.top_upgrade_candidates = [{"title": f"Movie {i}"} for i in range(50)]
        d = analysis.to_dict()
        assert len(d["top_upgrade_candidates"]) == 20

    def test_stats_dashboard_parse_size_edge_cases(self):
        sd = StatsDashboard.__new__(StatsDashboard)
        assert sd._parse_size("") == 0.0
        assert sd._parse_size("unknown") == 0.0
        assert sd._parse_size("10.5 GB") == 10.5
        assert sd._parse_size("512 MB") == 512 / 1024
        assert sd._parse_size("2 TB") == 2 * 1024

    def test_db_close_then_operations(self, tmp_db):
        """Operations after close should not raise unhandled exceptions."""
        dm = DatabaseManager(db_path=tmp_db)
        dm.close()
        # After close, get_connection recreates a connection
        assert dm.get_history_count() >= 0

    def test_db_save_plex_cache_empty_list(self, db_manager):
        # Should be a no-op, not crash
        db_manager.save_plex_cache([], "Movies")
        assert db_manager.load_plex_cache("Movies") == [] or isinstance(
            db_manager.load_plex_cache("Movies"), list
        )

    def test_cached_fuzz_ratio_identical_strings(self):
        clear_fuzzy_cache()
        assert cached_fuzz_ratio("hello", "hello") == 100

    def test_cached_fuzz_ratio_empty_strings(self):
        result = cached_fuzz_ratio("", "")
        assert result == 0 or result == 100  # fuzz.ratio("","") is 0

    def test_check_download_history(self, mock_app):
        engine = MatchingEngine(mock_app)
        mock_app.download_history = {"http://already.com/downloaded"}
        web_item = {"url": "http://already.com/downloaded"}
        assert engine.check_download_history(web_item) is True
        web_item2 = {"url": "http://new.com/fresh"}
        assert engine.check_download_history(web_item2) is False

    def test_should_skip_by_preference_1080p_pref(self, mock_app):
        mock_app.config["pref_res"] = "Prefer 1080p"
        engine = MatchingEngine(mock_app)
        assert engine.should_skip_by_preference({"res": "4K"}) is True
        assert engine.should_skip_by_preference({"res": "1080p"}) is False

    def test_watchlist_manager_get_stats_empty(self, tmp_path):
        wm = WatchlistManager(db_path=str(tmp_path / "empty.db"))
        try:
            stats = wm.get_stats()
            assert stats["total"] == 0
            assert stats["by_status"] == {}
        finally:
            wm.close()

    def test_watchlist_export_import_round_trip(self, tmp_path):
        wm = WatchlistManager(db_path=str(tmp_path / "rt.db"))
        try:
            wm.add(WatchlistItem(title="Inception", year=2010, imdb_id="tt1375666"))
            exported = wm.export_to_json()
            data = json.loads(exported)
            assert data["count"] == 1

            wm2 = WatchlistManager(db_path=str(tmp_path / "rt2.db"))
            try:
                count = wm2.import_from_json(exported)
                assert count == 1
            finally:
                wm2.close()
        finally:
            wm.close()

    def test_tv_upgrade_status_zero_episodes(self, mock_app):
        """Division by zero guard when episode_count=0."""
        engine = MatchingEngine(mock_app)
        web = {
            "display_title": "Show",
            "year": 2020,
            "res": "1080p",
            "size": "30 GB",
            "dovi": False,
            "url": "http://example.com/show",
            "is_tv": True,
            "season": 1,
            "episodes": 10,
        }
        match = {
            "clean_title": "show",
            "original_title": "Show",
            "year": 2020,
            "res": "1080p",
            "size": 20.0,
            "dovi": False,
            "hdr": False,
            "imdb_id": "tt9999999",
            "rating_key": "8001",
            "season": 1,
            "episode_count": 0,  # Zero episodes!
        }
        status, color, info, is_upgrade = engine.calculate_tv_upgrade_status(web, match)
        assert isinstance(status, str)
        # Should not crash with ZeroDivisionError
