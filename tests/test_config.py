"""Comprehensive tests for backend/config.py module.

Covers:
- AppConfig TypedDict structure
- _DEFAULT_CONFIG / DEFAULT_CONFIG contents and alias
- get_default_config() deep copy behavior
- validate_config() clamping, boundary, and edge-case logic
- SETTINGS_PRESETS structure and values
- File path constants resolution
- UI and Network constants
"""

import copy
import os
import sys

import pytest

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.config import (
    AppConfig,
    _DEFAULT_CONFIG,
    DEFAULT_CONFIG,
    get_default_config,
    validate_config,
    SETTINGS_PRESETS,
    CACHE_FILE,
    HISTORY_FILE,
    CONFIG_FILE,
    LOG_FILE,
    _BASE_DIR,
    TOOLTIP_DELAY_MS,
    DEFAULT_BUTTON_WIDTH,
    DEFAULT_WINDOW_WIDTH_PERCENT,
    DEFAULT_WINDOW_HEIGHT_PERCENT,
    PROGRESS_UPDATE_THROTTLE_MS,
    API_RATE_LIMIT_DELAY,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RETRY_BACKOFF_FACTOR,
)


# ---------------------------------------------------------------------------
# Helper: full list of keys expected in the default config
# ---------------------------------------------------------------------------
EXPECTED_DEFAULT_KEYS = {
    # Plex connection
    "plex_url", "plex_token", "plex_server_id", "plex_connection_mode",
    "plex_username", "plex_password", "plex_server_name",
    # API keys
    "tmdb_api_key", "omdb_api_key", "use_tmdb",
    # Size / resolution
    "min_size_mb", "pref_res",
    # Display
    "show_rating", "show_votes", "show_rt", "show_rg", "show_nf", "show_links", "show_genres",
    # Cache
    "cache_duration", "plex_refresh_mode", "plex_invalidate_on_new_content",
    # Filtering
    "ignore_keywords",
    # Upgrade rules
    "upgrade_sensitivity", "rule_1080_4k", "rule_1080_4k_size",
    "rule_1080_1080", "rule_4k_4k", "rule_dv", "strict_resolution",
    # Libraries
    "movie_libs", "tv_libs", "known_libraries",
    # JDownloader
    "jd_enabled", "jd_method", "jd_folder", "jd_movies_folder", "jd_tv_folder",
    "jd_email", "jd_password", "jd_device",
    # Source / filtering
    "exclude_720p", "source_2160p", "source_remux", "source_tv_packs",
    # DDLBase / Cuty
    "ddlbase_enabled", "ddlbase_manual_resolution_timeout",
    "cuty_email", "cuty_password",
    # Adit-HD
    "adithd_enabled", "adithd_username", "adithd_password",
    "adithd_auto_reply", "adithd_preferred_host",
    # Scanner
    "base_url", "scheduler_only_when_idle",
    # Scheduler
    "scheduler_enabled", "scheduler_interval", "last_scan_time",
    # Background pre-cache scanning
    "background_scan_enabled", "background_scan_interval_hours",
    "background_scan_pages", "background_scan_sources",
    "background_scan_retain_days", "background_scan_last_run",
    # Debug / logging
    "debug_mode", "verbose_logging", "clear_logs_startup", "scan_threads",
    # Matching thresholds
    "tv_match_threshold", "low_match_threshold", "movie_match_threshold",
    "year_tolerance",
    # Appearance
    "tile_columns", "theme_mode",
    # System tray / startup
    "enable_system_tray", "minimize_to_tray", "start_minimized",
    "auto_connect_plex",
    # Plex account (remote)
    "plex_connection_mode", "plex_selected_server",
    # Auto-Grab
    "auto_grab_enabled", "auto_grab_min_rating", "auto_grab_min_votes",
    "auto_grab_genres", "auto_grab_exclude_genres", "auto_grab_languages",
    "auto_grab_statuses",
    # Notifications
    "desktop_notifications", "discord_webhook", "discord_username",
    "slack_webhook", "pushover_user", "pushover_token",
    "webhook_url", "webhook_method",
    "email_enabled", "smtp_host", "smtp_port", "smtp_username",
    "smtp_password", "email_from", "email_to", "smtp_tls",
}


# ===================================================================
# 1. DEFAULT CONFIG - keys and alias
# ===================================================================

class TestDefaultConfig:
    """Tests for _DEFAULT_CONFIG, DEFAULT_CONFIG alias, and key presence."""

    def test_default_config_contains_all_expected_keys(self):
        """Every key listed in EXPECTED_DEFAULT_KEYS must be present."""
        missing = EXPECTED_DEFAULT_KEYS - set(_DEFAULT_CONFIG.keys())
        assert not missing, f"Missing keys in _DEFAULT_CONFIG: {missing}"

    def test_default_config_has_no_unexpected_keys(self):
        """_DEFAULT_CONFIG should not contain keys outside our expected set."""
        extra = set(_DEFAULT_CONFIG.keys()) - EXPECTED_DEFAULT_KEYS
        assert not extra, f"Unexpected keys in _DEFAULT_CONFIG: {extra}"

    def test_default_config_alias_is_same_object(self):
        """DEFAULT_CONFIG must be the exact same object as _DEFAULT_CONFIG."""
        assert DEFAULT_CONFIG is _DEFAULT_CONFIG

    def test_default_config_plex_url(self):
        assert _DEFAULT_CONFIG["plex_url"] == "http://127.0.0.1:32400"

    def test_default_config_pref_res(self):
        assert _DEFAULT_CONFIG["pref_res"] == "Prefer 4K"

    def test_default_config_scheduler_interval(self):
        assert _DEFAULT_CONFIG["scheduler_interval"] == 24

    def test_default_config_cache_defaults(self):
        assert _DEFAULT_CONFIG["cache_duration"] == 4
        assert _DEFAULT_CONFIG["plex_refresh_mode"] == "auto"
        assert _DEFAULT_CONFIG["plex_invalidate_on_new_content"] is True

    def test_default_config_scan_threads(self):
        assert _DEFAULT_CONFIG["scan_threads"] == 10

    def test_default_config_thresholds(self):
        assert _DEFAULT_CONFIG["tv_match_threshold"] == 90
        assert _DEFAULT_CONFIG["low_match_threshold"] == 75
        assert _DEFAULT_CONFIG["movie_match_threshold"] == 85

    def test_default_config_year_tolerance(self):
        assert _DEFAULT_CONFIG["year_tolerance"] == 1

    def test_default_config_notification_defaults(self):
        assert _DEFAULT_CONFIG["desktop_notifications"] is True
        assert _DEFAULT_CONFIG["discord_username"] == "ScanHound"
        assert _DEFAULT_CONFIG["webhook_method"] == "POST"
        assert _DEFAULT_CONFIG["smtp_port"] == 587
        assert _DEFAULT_CONFIG["smtp_tls"] is True

    def test_default_config_lists_are_lists(self):
        """Library fields must be lists."""
        assert isinstance(_DEFAULT_CONFIG["movie_libs"], list)
        assert isinstance(_DEFAULT_CONFIG["tv_libs"], list)
        assert isinstance(_DEFAULT_CONFIG["known_libraries"], list)


# ===================================================================
# 2. get_default_config() - deep copy / mutation safety
# ===================================================================

class TestGetDefaultConfig:
    """Tests for get_default_config() returning independent deep copies."""

    def test_returns_dict(self):
        cfg = get_default_config()
        assert isinstance(cfg, dict)

    def test_returns_copy_not_original(self):
        cfg = get_default_config()
        assert cfg is not _DEFAULT_CONFIG

    def test_two_calls_return_independent_copies(self):
        cfg1 = get_default_config()
        cfg2 = get_default_config()
        assert cfg1 is not cfg2

    def test_mutating_copy_does_not_affect_original(self):
        cfg = get_default_config()
        cfg["min_size_mb"] = 99999
        assert _DEFAULT_CONFIG["min_size_mb"] != 99999

    def test_mutating_nested_list_does_not_affect_original(self):
        cfg = get_default_config()
        original_movie_libs = list(_DEFAULT_CONFIG["movie_libs"])
        cfg["movie_libs"].append("MUTATED")
        assert _DEFAULT_CONFIG["movie_libs"] == original_movie_libs

    def test_copy_equals_original_values(self):
        cfg = get_default_config()
        assert cfg == _DEFAULT_CONFIG

    def test_copy_has_same_keys(self):
        cfg = get_default_config()
        assert set(cfg.keys()) == set(_DEFAULT_CONFIG.keys())


# ===================================================================
# 3. validate_config() - clamping and sanitization
# ===================================================================

class TestValidateConfigClampNegatives:
    """validate_config must clamp negative values to their floor."""

    def test_negative_min_size_mb_clamped_to_zero(self):
        result = validate_config({"min_size_mb": -10})
        assert result["min_size_mb"] == 0

    def test_negative_scheduler_interval_clamped_to_one(self):
        result = validate_config({"scheduler_interval": -5})
        assert result["scheduler_interval"] == 1

    def test_zero_scheduler_interval_clamped_to_one(self):
        result = validate_config({"scheduler_interval": 0})
        assert result["scheduler_interval"] == 1

    def test_negative_cache_duration_clamped_to_zero(self):
        result = validate_config({"cache_duration": -3})
        assert result["cache_duration"] == 0

    def test_negative_upgrade_sensitivity_clamped_to_zero(self):
        result = validate_config({"upgrade_sensitivity": -1})
        assert result["upgrade_sensitivity"] == 0

    def test_negative_scan_threads_clamped_to_one(self):
        result = validate_config({"scan_threads": -10})
        assert result["scan_threads"] == 1

    def test_zero_scan_threads_clamped_to_one(self):
        result = validate_config({"scan_threads": 0})
        assert result["scan_threads"] == 1


class TestValidateConfigThresholds:
    """Threshold fields must be clamped to 0-100."""

    @pytest.mark.parametrize("key", [
        "tv_match_threshold",
        "low_match_threshold",
        "movie_match_threshold",
    ])
    def test_threshold_negative_clamped_to_zero(self, key):
        result = validate_config({key: -50})
        assert result[key] == 0

    @pytest.mark.parametrize("key", [
        "tv_match_threshold",
        "low_match_threshold",
        "movie_match_threshold",
    ])
    def test_threshold_above_100_clamped(self, key):
        result = validate_config({key: 200})
        assert result[key] == 100

    @pytest.mark.parametrize("key", [
        "tv_match_threshold",
        "low_match_threshold",
        "movie_match_threshold",
    ])
    def test_threshold_at_zero_stays(self, key):
        result = validate_config({key: 0})
        assert result[key] == 0

    @pytest.mark.parametrize("key", [
        "tv_match_threshold",
        "low_match_threshold",
        "movie_match_threshold",
    ])
    def test_threshold_at_100_stays(self, key):
        result = validate_config({key: 100})
        assert result[key] == 100

    @pytest.mark.parametrize("key", [
        "tv_match_threshold",
        "low_match_threshold",
        "movie_match_threshold",
    ])
    def test_threshold_mid_range_preserved(self, key):
        result = validate_config({key: 50})
        assert result[key] == 50


class TestValidateConfigYearTolerance:
    """year_tolerance must be clamped to 0-10."""

    def test_negative_clamped_to_zero(self):
        result = validate_config({"year_tolerance": -3})
        assert result["year_tolerance"] == 0

    def test_above_10_clamped(self):
        result = validate_config({"year_tolerance": 25})
        assert result["year_tolerance"] == 10

    def test_zero_stays(self):
        result = validate_config({"year_tolerance": 0})
        assert result["year_tolerance"] == 0

    def test_ten_stays(self):
        result = validate_config({"year_tolerance": 10})
        assert result["year_tolerance"] == 10

    def test_mid_range_preserved(self):
        result = validate_config({"year_tolerance": 5})
        assert result["year_tolerance"] == 5


class TestValidateConfigScanThreads:
    """scan_threads must be clamped to 1-50."""

    def test_above_50_clamped(self):
        result = validate_config({"scan_threads": 100})
        assert result["scan_threads"] == 50

    def test_at_50_stays(self):
        result = validate_config({"scan_threads": 50})
        assert result["scan_threads"] == 50

    def test_at_1_stays(self):
        result = validate_config({"scan_threads": 1})
        assert result["scan_threads"] == 1

    def test_mid_range_preserved(self):
        result = validate_config({"scan_threads": 25})
        assert result["scan_threads"] == 25

    def test_scan_threads_none_left_unchanged(self):
        """When scan_threads is absent, validate_config should not add it."""
        result = validate_config({})
        assert "scan_threads" not in result


class TestValidateConfigPreservesValid:
    """validate_config must not alter values that are already within range."""

    def test_valid_full_config_round_trips(self):
        cfg = get_default_config()
        cleaned = validate_config(cfg)
        # Every numeric field should be unchanged
        for key in ("min_size_mb", "scheduler_interval", "scan_threads",
                     "cache_duration", "upgrade_sensitivity",
                     "tv_match_threshold", "low_match_threshold",
                     "movie_match_threshold", "year_tolerance"):
            assert cleaned[key] == cfg[key], f"{key} was altered unexpectedly"

    def test_non_numeric_fields_pass_through(self):
        cfg = {"plex_url": "http://example.com", "plex_token": "abc123"}
        result = validate_config(cfg)
        assert result["plex_url"] == "http://example.com"
        assert result["plex_token"] == "abc123"

    def test_returns_new_dict(self):
        original = {"min_size_mb": 200}
        result = validate_config(original)
        assert result is not original

    def test_original_not_mutated(self):
        original = {"min_size_mb": -5}
        validate_config(original)
        assert original["min_size_mb"] == -5


class TestValidateConfigEdgeCases:
    """Edge cases: type coercion, None values, string-encoded numbers, extremes."""

    def test_string_scan_threads_coerced(self):
        """scan_threads that is a string number should be int-coerced."""
        result = validate_config({"scan_threads": "25"})
        assert result["scan_threads"] == 25

    def test_string_threshold_coerced(self):
        result = validate_config({"tv_match_threshold": "80"})
        assert result["tv_match_threshold"] == 80

    def test_float_scan_threads_truncated(self):
        result = validate_config({"scan_threads": 10.7})
        assert result["scan_threads"] == 10

    def test_float_threshold_truncated(self):
        result = validate_config({"movie_match_threshold": 85.9})
        assert result["movie_match_threshold"] == 85

    def test_none_scan_threads_skipped(self):
        """If scan_threads is None the guard should skip clamping."""
        result = validate_config({"scan_threads": None})
        # scan_threads is not None guard: `if scan_threads is not None` --
        # passing None means the int() call is skipped.
        assert result["scan_threads"] is None

    def test_none_threshold_skipped(self):
        """Thresholds with value None should remain None (guard: val is not None)."""
        result = validate_config({"tv_match_threshold": None})
        assert result["tv_match_threshold"] is None

    def test_none_year_tolerance_skipped(self):
        result = validate_config({"year_tolerance": None})
        assert result["year_tolerance"] is None

    def test_extremely_large_min_size_mb_preserved(self):
        result = validate_config({"min_size_mb": 999999})
        assert result["min_size_mb"] == 999999

    def test_extremely_large_scheduler_interval_preserved(self):
        result = validate_config({"scheduler_interval": 100000})
        assert result["scheduler_interval"] == 100000

    def test_empty_config_returns_empty(self):
        result = validate_config({})
        assert result == {}

    def test_extra_unknown_keys_pass_through(self):
        result = validate_config({"unknown_key": "value", "another": 42})
        assert result["unknown_key"] == "value"
        assert result["another"] == 42

    def test_min_size_mb_zero_stays(self):
        result = validate_config({"min_size_mb": 0})
        assert result["min_size_mb"] == 0

    def test_cache_duration_zero_stays(self):
        result = validate_config({"cache_duration": 0})
        assert result["cache_duration"] == 0

    def test_invalid_plex_refresh_mode_resets_to_auto(self):
        result = validate_config({"plex_refresh_mode": "bad_mode"})
        assert result["plex_refresh_mode"] == "auto"

    def test_upgrade_sensitivity_zero_stays(self):
        result = validate_config({"upgrade_sensitivity": 0})
        assert result["upgrade_sensitivity"] == 0


class TestValidateConfigMultipleFields:
    """Validate that multiple fields are all clamped in a single call."""

    def test_all_fields_clamped_simultaneously(self):
        bad_config = {
            "min_size_mb": -100,
            "scheduler_interval": -10,
            "scan_threads": 200,
            "cache_duration": -5,
            "upgrade_sensitivity": -1,
            "tv_match_threshold": 999,
            "low_match_threshold": -50,
            "movie_match_threshold": 150,
            "year_tolerance": 99,
        }
        result = validate_config(bad_config)
        assert result["min_size_mb"] == 0
        assert result["scheduler_interval"] == 1
        assert result["scan_threads"] == 50
        assert result["cache_duration"] == 0
        assert result["upgrade_sensitivity"] == 0
        assert result["tv_match_threshold"] == 100
        assert result["low_match_threshold"] == 0
        assert result["movie_match_threshold"] == 100
        assert result["year_tolerance"] == 10


# ===================================================================
# 4. SETTINGS_PRESETS
# ===================================================================

class TestSettingsPresets:
    """Tests for SETTINGS_PRESETS structure and contents."""

    EXPECTED_PRESET_NAMES = {
        "Aggressive Upgrades",
        "Conservative",
        "4K Only",
        "Quality Seeker",
        "Balanced",
    }

    def test_all_expected_presets_present(self):
        assert set(SETTINGS_PRESETS.keys()) == self.EXPECTED_PRESET_NAMES

    def test_each_preset_has_description(self):
        for name, preset in SETTINGS_PRESETS.items():
            assert "description" in preset, f"Preset '{name}' missing 'description'"
            assert isinstance(preset["description"], str)
            assert len(preset["description"]) > 0

    def test_each_preset_has_upgrade_rules(self):
        rule_keys = {"rule_dv", "rule_1080_4k", "upgrade_sensitivity", "min_size_mb"}
        for name, preset in SETTINGS_PRESETS.items():
            for key in rule_keys:
                assert key in preset, f"Preset '{name}' missing key '{key}'"

    def test_preset_values_are_valid_for_validate_config(self):
        """Applying validate_config to each preset should not alter its numeric values."""
        for name, preset in SETTINGS_PRESETS.items():
            cleaned = validate_config(preset)
            for key in preset:
                if key == "description":
                    continue
                assert cleaned[key] == preset[key], (
                    f"Preset '{name}' key '{key}' changed from {preset[key]} to {cleaned[key]}"
                )

    def test_aggressive_preset_specifics(self):
        p = SETTINGS_PRESETS["Aggressive Upgrades"]
        assert p["upgrade_sensitivity"] == 1
        assert p["rule_1080_4k_size"] is True
        assert p["strict_resolution"] is False

    def test_conservative_preset_specifics(self):
        p = SETTINGS_PRESETS["Conservative"]
        assert p["upgrade_sensitivity"] == 10
        assert p["rule_1080_1080"] is False
        assert p["strict_resolution"] is True

    def test_4k_only_preset_specifics(self):
        p = SETTINGS_PRESETS["4K Only"]
        assert p["pref_res"] == "Prefer 4K"
        assert p["rule_1080_1080"] is False
        assert p["min_size_mb"] == 1000

    def test_balanced_preset_matches_defaults(self):
        """Balanced preset should align with the default config's upgrade rules."""
        p = SETTINGS_PRESETS["Balanced"]
        assert p["upgrade_sensitivity"] == _DEFAULT_CONFIG["upgrade_sensitivity"]
        assert p["min_size_mb"] == _DEFAULT_CONFIG["min_size_mb"]


# ===================================================================
# 5. File path constants
# ===================================================================

class TestFilePathConstants:
    """Tests for file path resolution."""

    def test_base_dir_is_project_root(self):
        """_BASE_DIR should be the parent of backend/."""
        # backend/ should be a child of _BASE_DIR
        assert os.path.isdir(os.path.join(_BASE_DIR, "backend"))

    def test_cache_file_path(self):
        assert CACHE_FILE.endswith("crawler.db")
        assert os.path.isabs(CACHE_FILE)

    def test_history_file_path(self):
        assert HISTORY_FILE == os.path.join(_BASE_DIR, "download_history.json")

    def test_config_file_path(self):
        assert CONFIG_FILE.endswith("config.json")
        assert os.path.isabs(CONFIG_FILE)

    def test_log_file_path(self):
        assert LOG_FILE.endswith("scanner.log")
        assert os.path.isabs(LOG_FILE)

    def test_all_paths_are_absolute(self):
        for path in (CACHE_FILE, HISTORY_FILE, CONFIG_FILE, LOG_FILE):
            assert os.path.isabs(path), f"{path} is not absolute"

    def test_paths_in_base_dir_or_data_dir(self):
        assert os.path.dirname(HISTORY_FILE) == _BASE_DIR
        # App dir is capitalized on Windows (%LOCALAPPDATA%\ScanHound) but
        # lowercased on Linux/macOS (~/.local/share/scanhound), so match
        # case-insensitively.
        assert "scanhound" in LOG_FILE.lower()
        assert "scanhound" in CACHE_FILE.lower()


# ===================================================================
# 6. UI Constants
# ===================================================================

class TestUIConstants:
    """Tests for UI constant values."""

    def test_tooltip_delay(self):
        assert TOOLTIP_DELAY_MS == 500

    def test_default_button_width(self):
        assert DEFAULT_BUTTON_WIDTH == 120

    def test_window_width_percent(self):
        assert DEFAULT_WINDOW_WIDTH_PERCENT == 0.9

    def test_window_height_percent(self):
        assert DEFAULT_WINDOW_HEIGHT_PERCENT == 0.9

    def test_progress_update_throttle(self):
        assert PROGRESS_UPDATE_THROTTLE_MS == 100


# ===================================================================
# 7. Network Constants
# ===================================================================

class TestNetworkConstants:
    """Tests for network constant values."""

    def test_api_rate_limit_delay(self):
        assert API_RATE_LIMIT_DELAY == 0.25

    def test_request_timeout(self):
        assert REQUEST_TIMEOUT == 10

    def test_max_retries(self):
        assert MAX_RETRIES == 3

    def test_retry_backoff_factor(self):
        assert RETRY_BACKOFF_FACTOR == 2


# ===================================================================
# 8. AppConfig TypedDict structure
# ===================================================================

class TestAppConfigTypedDict:
    """Basic structural checks on the AppConfig TypedDict."""

    def test_appconfig_is_a_type(self):
        assert isinstance(AppConfig, type)

    def test_appconfig_annotations_include_plex_url(self):
        annotations = AppConfig.__annotations__
        assert "plex_url" in annotations

    def test_appconfig_total_false(self):
        """AppConfig is defined with total=False so all fields are optional."""
        assert AppConfig.__total__ is False
