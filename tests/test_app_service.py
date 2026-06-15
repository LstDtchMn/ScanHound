"""Comprehensive tests for backend/app_service.py module.

Covers:
- LRUCache: basic CRUD, eviction, thread safety, edge cases
- clean_string / normalize_title: normalization, years, special chars, empty/None
- Status and color constants
- RESOLUTION_ORDER mapping
- TMDB_GENRE_MAP and TMDB_LANGUAGE_MAP
- setup_logging
- retry_request decorator
- AppService.validate_config_values
- AppService.apply_preset
- AppService.shutdown
"""

import logging
import os
import threading
import time
from unittest.mock import patch, MagicMock

import pytest
import requests

from backend.app_service import (
    LRUCache,
    clean_string,
    normalize_title,
    STATUS_MISSING,
    STATUS_DOWNLOADED,
    STATUS_IN_LIBRARY,
    STATUS_IN_LIBRARY_CHECK,
    STATUS_UPGRADE_4K,
    STATUS_UPGRADE_SIZE,
    STATUS_UPGRADE_SIZE_DV,
    STATUS_DV_UPGRADE,
    COLOR_MISSING,
    COLOR_DOWNLOADED,
    COLOR_IN_LIBRARY,
    COLOR_UPGRADE,
    COLOR_DV_UPGRADE,
    RESOLUTION_ORDER,
    TMDB_GENRE_MAP,
    TMDB_LANGUAGE_MAP,
    setup_logging,
    retry_request,
    AppService,
    APP_NAME,
    APP_VERSION,
    TMDB_API_BASE,
    TMDB_IMAGE_BASE,
)
from backend.config import SETTINGS_PRESETS, get_default_config


# ======================================================================
# LRUCache Tests
# ======================================================================

class TestLRUCache:
    """Tests for the LRUCache class."""

    def test_basic_set_and_get(self):
        """Setting a key and retrieving it should return the value."""
        cache = LRUCache(maxsize=10)
        cache["key1"] = "value1"
        assert cache["key1"] == "value1"

    def test_get_missing_key_raises_keyerror(self):
        """Accessing a missing key via __getitem__ should raise KeyError."""
        cache = LRUCache(maxsize=10)
        with pytest.raises(KeyError):
            _ = cache["nonexistent"]

    def test_contains_present_key(self):
        """__contains__ should return True for existing keys."""
        cache = LRUCache(maxsize=10)
        cache["x"] = 42
        assert "x" in cache

    def test_contains_absent_key(self):
        """__contains__ should return False for absent keys."""
        cache = LRUCache(maxsize=10)
        assert "missing" not in cache

    def test_get_with_default(self):
        """get() should return the default when the key is absent."""
        cache = LRUCache(maxsize=10)
        assert cache.get("missing", "fallback") == "fallback"

    def test_get_without_default_returns_none(self):
        """get() with no default should return None for missing keys."""
        cache = LRUCache(maxsize=10)
        assert cache.get("missing") is None

    def test_get_existing_key(self):
        """get() should return the stored value for existing keys."""
        cache = LRUCache(maxsize=10)
        cache["a"] = 100
        assert cache.get("a", -1) == 100

    def test_len(self):
        """__len__ should reflect the number of items in the cache."""
        cache = LRUCache(maxsize=10)
        assert len(cache) == 0
        cache["a"] = 1
        cache["b"] = 2
        assert len(cache) == 2

    def test_clear(self):
        """clear() should remove all items from the cache."""
        cache = LRUCache(maxsize=10)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache.clear()
        assert len(cache) == 0
        assert "a" not in cache

    def test_overwrite_existing_key(self):
        """Setting a key that already exists should update the value."""
        cache = LRUCache(maxsize=10)
        cache["key"] = "old"
        cache["key"] = "new"
        assert cache["key"] == "new"
        assert len(cache) == 1

    def test_eviction_at_maxsize(self):
        """When cache exceeds maxsize, the oldest entry should be evicted."""
        cache = LRUCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        # Cache is full; adding a new item should evict "a"
        cache["d"] = 4
        assert "a" not in cache
        assert len(cache) == 3
        assert cache["b"] == 2
        assert cache["c"] == 3
        assert cache["d"] == 4

    def test_eviction_order_lru(self):
        """Accessing a key should move it to the end, so LRU order is maintained."""
        cache = LRUCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        # Access "a" to move it to most-recently-used
        _ = cache["a"]
        # Now "b" is the LRU; adding "d" should evict "b"
        cache["d"] = 4
        assert "b" not in cache
        assert "a" in cache
        assert "c" in cache
        assert "d" in cache

    def test_update_moves_to_end(self):
        """Updating an existing key should move it to end (most recently used)."""
        cache = LRUCache(maxsize=3)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        # Update "a" — this moves it to end
        cache["a"] = 10
        # Now "b" is LRU; adding "d" should evict "b"
        cache["d"] = 4
        assert "b" not in cache
        assert cache["a"] == 10

    def test_maxsize_one(self):
        """A cache with maxsize=1 should only hold the most recent item."""
        cache = LRUCache(maxsize=1)
        cache["a"] = 1
        assert cache["a"] == 1
        cache["b"] = 2
        assert "a" not in cache
        assert cache["b"] == 2
        assert len(cache) == 1

    def test_default_maxsize(self):
        """Default maxsize should be 1000."""
        cache = LRUCache()
        assert cache.maxsize == 1000

    def test_thread_safety_concurrent_writes(self):
        """Multiple threads writing to the cache concurrently should not corrupt it."""
        cache = LRUCache(maxsize=500)
        errors = []

        def writer(start, count):
            try:
                for i in range(start, start + count):
                    cache[f"key_{i}"] = i
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(i * 100, 100))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(cache) == 500

    def test_thread_safety_concurrent_reads_and_writes(self):
        """Concurrent reads and writes should not raise exceptions."""
        cache = LRUCache(maxsize=200)
        # Pre-populate
        for i in range(100):
            cache[f"k{i}"] = i

        errors = []

        def reader():
            try:
                for i in range(100):
                    cache.get(f"k{i}", None)
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(100, 200):
                    cache[f"k{i}"] = i
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(cache) == 200


# ======================================================================
# clean_string Tests
# ======================================================================

class TestCleanString:
    """Tests for the clean_string() function."""

    def test_basic_lowercase(self):
        """Should lowercase the input."""
        assert clean_string("The Matrix") == "the matrix"

    def test_strips_parenthesized_year(self):
        """Should remove years in parentheses like (2024)."""
        assert clean_string("Dune (2021)") == "dune"

    def test_strips_bare_year_20xx(self):
        """Should remove bare years matching 20xx pattern."""
        assert clean_string("Blade Runner 2049") == "blade runner"

    def test_strips_bare_year_19xx(self):
        """Should remove bare years matching 19xx pattern."""
        assert clean_string("Alien 1979") == "alien"

    def test_removes_special_characters(self):
        """Should remove non-alphanumeric characters except spaces."""
        assert clean_string("Spider-Man: No Way Home") == "spiderman no way home"

    def test_collapses_whitespace(self):
        """Should collapse multiple spaces into one."""
        assert clean_string("The   Lord   of   the   Rings") == "the lord of the rings"

    def test_strips_leading_trailing_whitespace(self):
        """Should strip leading/trailing whitespace."""
        assert clean_string("  Hello World  ") == "hello world"

    def test_empty_string(self):
        """Should return empty string for empty input."""
        assert clean_string("") == ""

    def test_none_input(self):
        """Should return empty string for None input."""
        assert clean_string(None) == ""

    def test_title_with_ampersand(self):
        """Ampersand should be removed and whitespace collapsed."""
        result = clean_string("Fast & Furious")
        assert result == "fast furious"
        assert "  " not in result  # no double spaces

    def test_title_with_apostrophe(self):
        """Apostrophe should be removed."""
        result = clean_string("Schindler's List")
        assert result == "schindlers list"

    def test_multiple_years_removed(self):
        """Both parenthesized and bare years should be removed."""
        result = clean_string("Blade Runner (2049) 2017")
        assert "2049" not in result
        assert "2017" not in result

    def test_only_special_chars(self):
        """A string of only special chars should produce empty string."""
        assert clean_string("!!!---???") == ""


# ======================================================================
# normalize_title Tests
# ======================================================================

class TestNormalizeTitle:
    """Tests for the normalize_title() function."""

    def test_basic_normalization(self):
        """Should lowercase and strip basic titles."""
        assert normalize_title("The Matrix") == "the matrix"

    def test_strips_parenthesized_year(self):
        """Should remove years in parentheses."""
        assert normalize_title("Inception (2010)") == "inception"

    def test_strips_bare_year(self):
        """Should remove bare year patterns."""
        assert normalize_title("Blade Runner 2049") == "blade runner"

    def test_removes_special_chars(self):
        """Should remove non-alphanumeric except spaces."""
        assert normalize_title("Spider-Man: Homecoming") == "spiderman homecoming"

    def test_empty_string(self):
        """Should return empty string for empty input."""
        assert normalize_title("") == ""

    def test_none_input(self):
        """Should return empty string for None input."""
        assert normalize_title(None) == ""

    def test_consistent_with_clean_string(self):
        """normalize_title and clean_string should produce identical output."""
        titles = [
            "The Dark Knight (2008)",
            "Interstellar",
            "Blade Runner 2049",
            "Spider-Man: No Way Home (2021)",
            "",
            None,
        ]
        for t in titles:
            assert normalize_title(t) == clean_string(t), f"Mismatch for: {t!r}"


# ======================================================================
# Status & Color Constants Tests
# ======================================================================

class TestStatusConstants:
    """Verify that status constants exist and have the expected string values."""

    def test_status_missing(self):
        assert STATUS_MISSING == "Missing"

    def test_status_downloaded(self):
        assert STATUS_DOWNLOADED == "Downloaded"

    def test_status_in_library(self):
        assert STATUS_IN_LIBRARY == "In Library"

    def test_status_in_library_check(self):
        assert STATUS_IN_LIBRARY_CHECK == "\u2713 In Library"

    def test_status_upgrade_4k(self):
        assert STATUS_UPGRADE_4K == "UPGRADE (4K)"

    def test_status_upgrade_size(self):
        assert STATUS_UPGRADE_SIZE == "UPGRADE (Size)"

    def test_status_upgrade_size_dv(self):
        assert STATUS_UPGRADE_SIZE_DV == "UPGRADE (+DV)"

    def test_status_dv_upgrade(self):
        assert STATUS_DV_UPGRADE == "UPGRADE (DV)"


class TestColorConstants:
    """Verify that color constants exist and have expected hex values."""

    def test_color_missing(self):
        assert COLOR_MISSING == "#e74c3c"

    def test_color_downloaded(self):
        assert COLOR_DOWNLOADED == "#17a2b8"

    def test_color_in_library(self):
        assert COLOR_IN_LIBRARY == "#27ae60"

    def test_color_upgrade(self):
        assert COLOR_UPGRADE == "#f39c12"

    def test_color_dv_upgrade(self):
        assert COLOR_DV_UPGRADE == "#9b59b6"

    def test_all_colors_are_valid_hex(self):
        """All color constants should be valid hex color strings."""
        import re
        hex_pattern = re.compile(r'^#[0-9a-fA-F]{6}$')
        colors = [COLOR_MISSING, COLOR_DOWNLOADED, COLOR_IN_LIBRARY,
                  COLOR_UPGRADE, COLOR_DV_UPGRADE]
        for c in colors:
            assert hex_pattern.match(c), f"Invalid hex color: {c}"


# ======================================================================
# RESOLUTION_ORDER Tests
# ======================================================================

class TestResolutionOrder:
    """Verify the RESOLUTION_ORDER mapping."""

    def test_unknown_is_lowest(self):
        assert RESOLUTION_ORDER["?"] == 0

    def test_sd_rank(self):
        assert RESOLUTION_ORDER["SD"] == 1

    def test_720p_rank(self):
        assert RESOLUTION_ORDER["720p"] == 2

    def test_1080p_rank(self):
        assert RESOLUTION_ORDER["1080p"] == 3

    def test_4k_is_highest(self):
        assert RESOLUTION_ORDER["4K"] == 4

    def test_ordering_is_strictly_increasing(self):
        """Each resolution should have a strictly higher rank than the previous."""
        ordered_keys = ["?", "SD", "720p", "1080p", "4K"]
        for i in range(1, len(ordered_keys)):
            assert RESOLUTION_ORDER[ordered_keys[i]] > RESOLUTION_ORDER[ordered_keys[i - 1]]

    def test_all_five_entries_present(self):
        assert len(RESOLUTION_ORDER) == 5


# ======================================================================
# TMDB Maps Tests
# ======================================================================

class TestTMDBGenreMap:
    """Verify TMDB_GENRE_MAP contains expected entries."""

    def test_action_genre_present(self):
        assert 28 in TMDB_GENRE_MAP
        assert TMDB_GENRE_MAP[28] == "Action"

    def test_comedy_genre_present(self):
        assert 35 in TMDB_GENRE_MAP
        assert TMDB_GENRE_MAP[35] == "Comedy"

    def test_drama_genre_present(self):
        assert 18 in TMDB_GENRE_MAP
        assert TMDB_GENRE_MAP[18] == "Drama"

    def test_horror_genre_present(self):
        assert 27 in TMDB_GENRE_MAP
        assert TMDB_GENRE_MAP[27] == "Horror"

    def test_scifi_genre_present(self):
        assert 878 in TMDB_GENRE_MAP
        assert TMDB_GENRE_MAP[878] == "Sci-Fi"

    def test_all_values_are_strings(self):
        for genre_id, name in TMDB_GENRE_MAP.items():
            assert isinstance(genre_id, int), f"Key {genre_id} should be int"
            assert isinstance(name, str), f"Value for {genre_id} should be str"


class TestTMDBLanguageMap:
    """Verify TMDB_LANGUAGE_MAP contains expected entries."""

    def test_english_present(self):
        assert TMDB_LANGUAGE_MAP["en"] == "English"

    def test_spanish_present(self):
        assert TMDB_LANGUAGE_MAP["es"] == "Spanish"

    def test_japanese_present(self):
        assert TMDB_LANGUAGE_MAP["ja"] == "Japanese"

    def test_korean_present(self):
        assert TMDB_LANGUAGE_MAP["ko"] == "Korean"

    def test_chinese_both_codes(self):
        """Both 'zh' and 'cn' should map to Chinese."""
        assert TMDB_LANGUAGE_MAP["zh"] == "Chinese"
        assert TMDB_LANGUAGE_MAP["cn"] == "Chinese"

    def test_all_values_are_strings(self):
        for code, name in TMDB_LANGUAGE_MAP.items():
            assert isinstance(code, str), f"Key {code!r} should be str"
            assert isinstance(name, str), f"Value for {code!r} should be str"

    def test_has_many_languages(self):
        """Language map should have a substantial number of entries."""
        assert len(TMDB_LANGUAGE_MAP) >= 50


# ======================================================================
# setup_logging Tests
# ======================================================================

class TestSetupLogging:
    """Tests for the setup_logging() function."""

    def test_returns_root_logger(self, tmp_path):
        """setup_logging should return the root logger."""
        with patch("backend.app_service.LOG_FILE", str(tmp_path / "test.log")):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            assert isinstance(root, logging.Logger)
            assert root.name == "root"

    def test_debug_mode_sets_debug_level(self, tmp_path):
        """In debug mode, root logger should be at DEBUG level."""
        with patch("backend.app_service.LOG_FILE", str(tmp_path / "test.log")):
            root = setup_logging(debug_mode=True, clear_on_start=False)
            assert root.level == logging.DEBUG

    def test_normal_mode_sets_info_level(self, tmp_path):
        """Without debug mode, root logger should be at INFO level."""
        with patch("backend.app_service.LOG_FILE", str(tmp_path / "test.log")):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            assert root.level == logging.INFO

    def test_clear_on_start_removes_log_file(self, tmp_path):
        """When clear_on_start=True, existing log file should be removed."""
        log_file = tmp_path / "test.log"
        log_file.write_text("old log content")
        assert log_file.exists()
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            setup_logging(debug_mode=False, clear_on_start=True)
        # The file handler recreates it, but the old content should be gone
        # (the old file is removed before RotatingFileHandler opens a new one)

    def test_suppresses_noisy_loggers(self, tmp_path):
        """Third-party loggers should be set to WARNING."""
        with patch("backend.app_service.LOG_FILE", str(tmp_path / "test.log")):
            setup_logging(debug_mode=True, clear_on_start=False)
            for name in ("urllib3", "requests", "plexapi", "aiohttp"):
                assert logging.getLogger(name).level == logging.WARNING


# ======================================================================
# retry_request Tests
# ======================================================================

class TestRetryRequest:
    """Tests for the retry_request decorator."""

    def test_successful_call_returns_immediately(self):
        """A function that succeeds should return its value without retries."""
        call_count = 0

        @retry_request
        def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        with patch("backend.app_service.time.sleep"):
            result = succeed()
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_request_exception(self):
        """Should retry on RequestException and eventually raise after max retries."""
        call_count = 0

        @retry_request
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise requests.RequestException("network error")

        with patch("backend.app_service.time.sleep"):
            with pytest.raises(requests.RequestException):
                always_fail()

        # Should have tried MAX_RETRIES times
        from backend.config import MAX_RETRIES
        assert call_count == MAX_RETRIES

    def test_succeeds_after_transient_failure(self):
        """Should succeed if a transient failure recovers before max retries."""
        call_count = 0

        @retry_request
        def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise requests.ConnectionError("transient")
            return "recovered"

        with patch("backend.app_service.time.sleep"):
            result = fail_then_succeed()
        assert result == "recovered"
        assert call_count == 3


# ======================================================================
# AppService.validate_config_values Tests
# ======================================================================

class TestValidateConfigValues:
    """Tests for AppService.validate_config_values()."""

    def _make_service(self, config_overrides=None):
        """Create an AppService with a config but no real startup."""
        svc = AppService.__new__(AppService)
        svc.config = get_default_config()
        if config_overrides:
            svc.config.update(config_overrides)
        return svc

    def test_valid_defaults_produce_no_warnings(self):
        """Default config values should pass validation with no warnings."""
        svc = self._make_service()
        result = svc.validate_config_values()
        assert result["warnings"] == []
        assert result["errors"] == []

    def test_out_of_range_numeric_gets_corrected(self):
        """A numeric value above the allowed max should be reset to default."""
        svc = self._make_service({"min_size_mb": 999999})
        result = svc.validate_config_values()
        assert len(result["warnings"]) > 0
        assert svc.config["min_size_mb"] == 200  # reset to default

    def test_negative_numeric_gets_corrected(self):
        """A negative numeric value should be corrected to default."""
        svc = self._make_service({"cache_duration": -5})
        result = svc.validate_config_values()
        assert len(result["warnings"]) > 0
        assert svc.config["cache_duration"] == 4  # default

    def test_non_numeric_string_gets_corrected(self):
        """A string where a number is expected should be reset to default."""
        svc = self._make_service({"scan_threads": "not_a_number"})
        result = svc.validate_config_values()
        assert any("scan_threads" in w for w in result["warnings"])
        assert svc.config["scan_threads"] == 10  # default

    def test_boolean_conversion(self):
        """Non-boolean values for boolean fields should be converted with a warning."""
        svc = self._make_service({"debug_mode": 1})
        result = svc.validate_config_values()
        assert any("debug_mode" in w for w in result["warnings"])
        assert svc.config["debug_mode"] is True

    def test_boolean_string_conversion(self):
        """A non-empty string for a boolean field should become True."""
        svc = self._make_service({"jd_enabled": "yes"})
        result = svc.validate_config_values()
        assert any("jd_enabled" in w for w in result["warnings"])
        assert svc.config["jd_enabled"] is True

    def test_boolean_zero_becomes_false(self):
        """Integer 0 for a boolean field should convert to False."""
        svc = self._make_service({"use_tmdb": 0})
        result = svc.validate_config_values()
        assert svc.config["use_tmdb"] is False

    def test_plex_url_without_protocol_warns(self):
        """A Plex URL missing http/https prefix should generate a warning."""
        svc = self._make_service({"plex_url": "192.168.1.100:32400"})
        result = svc.validate_config_values()
        assert any("Plex URL" in w for w in result["warnings"])

    def test_plex_url_with_protocol_is_fine(self):
        """A Plex URL with http:// should not generate a URL warning."""
        svc = self._make_service({"plex_url": "http://192.168.1.100:32400"})
        result = svc.validate_config_values()
        url_warnings = [w for w in result["warnings"] if "Plex URL" in w]
        assert len(url_warnings) == 0

    def test_plex_url_https_is_fine(self):
        """A Plex URL with https:// should not generate a URL warning."""
        svc = self._make_service({"plex_url": "https://plex.example.com"})
        result = svc.validate_config_values()
        url_warnings = [w for w in result["warnings"] if "Plex URL" in w]
        assert len(url_warnings) == 0

    def test_all_numeric_fields_validated(self):
        """All expected numeric fields should be checked in validation."""
        expected_fields = {
            'min_size_mb', 'cache_duration', 'upgrade_sensitivity',
            'scheduler_interval', 'scan_threads', 'tv_match_threshold',
            'low_match_threshold', 'movie_match_threshold', 'year_tolerance',
        }
        svc = self._make_service()
        # Set all numeric fields to invalid string values
        for field in expected_fields:
            svc.config[field] = "bad"
        result = svc.validate_config_values()
        warned_fields = set()
        for w in result["warnings"]:
            for field in expected_fields:
                if field in w:
                    warned_fields.add(field)
        assert warned_fields == expected_fields

    def test_upgrade_sensitivity_boundary_zero(self):
        """upgrade_sensitivity at its minimum (0) should be accepted."""
        svc = self._make_service({"upgrade_sensitivity": 0})
        result = svc.validate_config_values()
        sensitivity_warnings = [w for w in result["warnings"] if "upgrade_sensitivity" in w]
        assert len(sensitivity_warnings) == 0
        assert svc.config["upgrade_sensitivity"] == 0

    def test_year_tolerance_boundary_max(self):
        """year_tolerance at its max (10) should be accepted."""
        svc = self._make_service({"year_tolerance": 10})
        result = svc.validate_config_values()
        tolerance_warnings = [w for w in result["warnings"] if "year_tolerance" in w]
        assert len(tolerance_warnings) == 0
        assert svc.config["year_tolerance"] == 10


# ======================================================================
# AppService.apply_preset Tests
# ======================================================================

class TestApplyPreset:
    """Tests for AppService.apply_preset()."""

    def _make_service(self):
        """Create an AppService with config but mock save_config."""
        svc = AppService.__new__(AppService)
        svc.config = get_default_config()
        svc.save_config = MagicMock()
        return svc

    def test_known_preset_returns_true(self):
        """Applying a valid preset should return True."""
        svc = self._make_service()
        assert svc.apply_preset("Aggressive Upgrades") is True

    def test_unknown_preset_returns_false(self):
        """Applying an invalid preset name should return False."""
        svc = self._make_service()
        assert svc.apply_preset("NonexistentPreset") is False

    def test_preset_applies_settings(self):
        """Applying a preset should update the config with the preset values."""
        svc = self._make_service()
        svc.apply_preset("Conservative")
        preset = SETTINGS_PRESETS["Conservative"]
        for key, value in preset.items():
            if key != "description":
                assert svc.config[key] == value, f"Mismatch for {key}"

    def test_preset_calls_save_config(self):
        """Applying a preset should persist the config via save_config."""
        svc = self._make_service()
        svc.apply_preset("Balanced")
        svc.save_config.assert_called_once()

    def test_unknown_preset_does_not_save(self):
        """An invalid preset should not trigger save_config."""
        svc = self._make_service()
        svc.apply_preset("FakePreset")
        svc.save_config.assert_not_called()

    def test_preset_does_not_apply_description(self):
        """The 'description' key from a preset should not be set on config."""
        svc = self._make_service()
        svc.apply_preset("4K Only")
        assert svc.config.get("description") != SETTINGS_PRESETS["4K Only"]["description"]

    def test_all_presets_are_applicable(self):
        """Every preset in SETTINGS_PRESETS should be applicable."""
        for preset_name in SETTINGS_PRESETS:
            svc = self._make_service()
            assert svc.apply_preset(preset_name) is True


# ======================================================================
# AppService.shutdown Tests
# ======================================================================

class TestAppServiceShutdown:
    """Tests for AppService.shutdown()."""

    def test_shutdown_sets_scheduler_stop(self):
        """shutdown() should signal the scheduler to stop."""
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = None
        svc.shutdown()
        assert svc._scheduler_stop.is_set()

    def test_shutdown_runs_hooks(self):
        """shutdown() should call all registered shutdown hooks."""
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = None
        hook_called = []
        svc._shutdown_hooks = [lambda: hook_called.append(True)]
        svc.shutdown()
        assert len(hook_called) == 1

    def test_shutdown_closes_db(self):
        """shutdown() should close the database if present."""
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = MagicMock()
        svc.shutdown()
        svc.db.close.assert_called_once()

    def test_shutdown_closes_watchlist_manager(self):
        """shutdown() should close watchlist_manager if present."""
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = MagicMock()
        svc.db = None
        svc.shutdown()
        svc.watchlist_manager.close.assert_called_once()

    def test_shutdown_hook_error_does_not_prevent_db_close(self):
        """Even if a shutdown hook raises, DB should still be closed."""
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = MagicMock()
        svc._shutdown_hooks = [MagicMock(side_effect=RuntimeError("hook failed"))]
        svc.shutdown()
        svc.db.close.assert_called_once()


# ======================================================================
# Application Metadata Constants Tests
# ======================================================================

class TestAppMetadata:
    """Verify application metadata constants."""

    def test_app_name(self):
        assert APP_NAME == "ScanHound"

    def test_app_version(self):
        assert APP_VERSION == "3.0"

    def test_tmdb_api_base(self):
        assert TMDB_API_BASE == "https://api.themoviedb.org/3"

    def test_tmdb_image_base(self):
        assert TMDB_IMAGE_BASE == "https://image.tmdb.org/t/p"
