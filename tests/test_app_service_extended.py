"""Extended tests for backend/app_service.py to increase coverage.

Covers functions/methods NOT well covered by the existing test_app_service.py:
- normalize_title: empty string, title with year in parens, year as word, special chars, multiple spaces
- _get_idle_seconds: returns 0 on non-Windows (Linux)
- setup_logging: creates handlers, debug vs info, clear_on_start removes log file
- retry_request: success first try, fails then succeeds, all retries exhausted
- AppService.__init__: all attributes initialized
- AppService.startup: returns warnings list, initializes db/plex_manager/config
- AppService.shutdown: calls hooks, closes watchlist, closes db
- AppService.add_shutdown_hook: hooks list populated
- AppService.set_log_callback / log: callback invoked with message and level
- AppService.save_config: writes JSON atomically (with tmp_path by patching CONFIG_FILE)
- AppService.validate_config_values: numeric out of range, non-numeric, boolean, plex_url
- AppService.apply_preset: valid preset returns True, invalid returns False
- AppService.load_download_history: returns set of URLs from database
- AppService.set_scan_trigger: stores callback
"""

import json
import logging
import os
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

import backend.app_service as app_service_module
from backend.app_service import (
    normalize_title,
    _get_idle_seconds,
    setup_logging,
    retry_request,
    AppService,
    LRUCache,
)
from backend.database import DatabaseManager
from backend.config import (
    SETTINGS_PRESETS,
    get_default_config,
    CONFIG_FILE,
    LOG_FILE,
    MAX_RETRIES,
)


# ======================================================================
# normalize_title — Extended Tests
# ======================================================================


class TestNormalizeTitleExtended:
    """Extended tests for normalize_title() covering edge cases."""

    def test_empty_string(self):
        """Empty string should return empty string."""
        assert normalize_title("") == ""

    def test_title_with_year_in_parens(self):
        """Year in parentheses like (2024) should be stripped."""
        result = normalize_title("Oppenheimer (2023)")
        assert "2023" not in result
        assert result == "oppenheimer"

    def test_title_with_year_as_word(self):
        """Bare year matching 19xx/20xx should be stripped."""
        result = normalize_title("Blade Runner 2049")
        assert "2049" not in result
        assert result == "blade runner"

    def test_title_with_19xx_year(self):
        """Years in the 1900s should also be stripped."""
        result = normalize_title("Alien 1979")
        assert "1979" not in result
        assert result == "alien"

    def test_special_characters_removed(self):
        """Hyphens, colons, and other special characters should be stripped."""
        result = normalize_title("Spider-Man: Across the Spider-Verse")
        assert "-" not in result
        assert ":" not in result
        assert "spiderman across the spiderverse" == result

    def test_multiple_spaces_collapsed(self):
        """Multiple consecutive spaces should be collapsed to one."""
        result = normalize_title("The   Lord    of    the    Rings")
        assert "  " not in result
        assert result == "the lord of the rings"

    def test_leading_trailing_whitespace_stripped(self):
        """Leading and trailing whitespace should be stripped."""
        result = normalize_title("   Inception   ")
        assert result == "inception"

    def test_ampersand_removed(self):
        """Ampersand should be removed."""
        result = normalize_title("Fast & Furious")
        assert "&" not in result
        assert result == "fast furious"

    def test_apostrophe_removed(self):
        """Apostrophe should be removed."""
        result = normalize_title("Schindler's List")
        assert "'" not in result
        assert result == "schindlers list"

    def test_mixed_case_lowered(self):
        """Mixed case input should be lowered."""
        result = normalize_title("THE DARK KNIGHT")
        assert result == "the dark knight"

    def test_only_digits_year(self):
        """A title that is only a year should be preserved (e.g. '2001: A Space Odyssey')."""
        result = normalize_title("2001")
        assert result == "2001"

    def test_multiple_years(self):
        """Multiple year patterns should all be stripped."""
        result = normalize_title("Blade Runner (2049) 2017")
        assert "2049" not in result
        assert "2017" not in result

    def test_non_year_numbers_preserved(self):
        """Numbers that do not match year patterns should be preserved."""
        result = normalize_title("Ocean's 11")
        assert "11" in result

    def test_parenthesized_non_year_preserved(self):
        """Parenthesized text that is not a year should remain (parens removed)."""
        result = normalize_title("Alien (Director's Cut)")
        # The parens and apostrophe are removed but the words remain
        assert "directors cut" in result


# ======================================================================
# _get_idle_seconds Tests
# ======================================================================


class TestGetIdleSeconds:
    """Tests for the _get_idle_seconds() function."""

    def test_returns_zero_on_linux(self):
        """On Linux (non-Windows), _get_idle_seconds should return 0."""
        with patch("platform.system", return_value="Linux"):
            result = _get_idle_seconds()
        assert result == 0

    def test_returns_int(self):
        """Return value should be an integer."""
        result = _get_idle_seconds()
        assert isinstance(result, int)


# ======================================================================
# setup_logging — Extended Tests
# ======================================================================


class TestSetupLoggingExtended:
    """Extended tests for setup_logging()."""

    def test_creates_file_handler(self, tmp_path):
        """setup_logging should create a RotatingFileHandler."""
        log_file = tmp_path / "test.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            handler_types = [type(h).__name__ for h in root.handlers]
            assert "RotatingFileHandler" in handler_types

    def test_creates_console_handler(self, tmp_path):
        """setup_logging should create a StreamHandler."""
        log_file = tmp_path / "test.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            handler_types = [type(h).__name__ for h in root.handlers]
            assert "StreamHandler" in handler_types

    def test_exactly_two_handlers(self, tmp_path):
        """setup_logging should set up exactly two handlers."""
        log_file = tmp_path / "test.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            assert len(root.handlers) == 2

    def test_debug_mode_sets_debug_level(self, tmp_path):
        """In debug mode, the root logger level should be DEBUG."""
        log_file = tmp_path / "test.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            root = setup_logging(debug_mode=True, clear_on_start=False)
            assert root.level == logging.DEBUG
            for h in root.handlers:
                assert h.level == logging.DEBUG

    def test_info_mode_sets_info_level(self, tmp_path):
        """Without debug mode, the root logger level should be INFO."""
        log_file = tmp_path / "test.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            assert root.level == logging.INFO
            for h in root.handlers:
                assert h.level == logging.INFO

    def test_clear_on_start_removes_log_file(self, tmp_path):
        """When clear_on_start=True, the existing log file content should be gone."""
        log_file = tmp_path / "test.log"
        log_file.write_text("old log content\n")
        assert log_file.exists()
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            setup_logging(debug_mode=False, clear_on_start=True)
        # The old file was removed before the RotatingFileHandler creates a new one.
        # Verify the old content is gone.
        if log_file.exists():
            content = log_file.read_text()
            assert "old log content" not in content

    def test_clear_on_start_false_preserves_nothing_special(self, tmp_path):
        """When clear_on_start=False and no log file exists, should not error."""
        log_file = tmp_path / "nonexistent.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            root = setup_logging(debug_mode=False, clear_on_start=False)
            assert root is not None

    def test_suppresses_third_party_loggers(self, tmp_path):
        """Third-party loggers should be set to WARNING level."""
        log_file = tmp_path / "test.log"
        with patch("backend.app_service.LOG_FILE", str(log_file)):
            setup_logging(debug_mode=True, clear_on_start=False)
            for name in ("urllib3", "requests", "plexapi", "aiohttp"):
                assert logging.getLogger(name).level == logging.WARNING


# ======================================================================
# retry_request — Extended Tests
# ======================================================================


class TestRetryRequestExtended:
    """Extended tests for the retry_request decorator."""

    def test_successful_first_try(self):
        """A function that succeeds on the first call should not sleep."""
        call_count = 0

        @retry_request
        def succeed():
            nonlocal call_count
            call_count += 1
            return "result"

        with patch("backend.app_service.time.sleep") as mock_sleep:
            result = succeed()
        assert result == "result"
        assert call_count == 1
        mock_sleep.assert_not_called()

    def test_fails_then_succeeds(self):
        """Should retry after failure and succeed when the error resolves."""
        call_count = 0

        @retry_request
        def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise requests.ConnectionError("transient error")
            return "recovered"

        with patch("backend.app_service.time.sleep"):
            result = fail_twice()
        assert result == "recovered"
        assert call_count == 3

    def test_all_retries_exhausted_raises(self):
        """When all retries are exhausted, the exception should be raised."""
        call_count = 0

        @retry_request
        def always_fail():
            nonlocal call_count
            call_count += 1
            raise requests.RequestException("permanent failure")

        with patch("backend.app_service.time.sleep"):
            with pytest.raises(requests.RequestException, match="permanent failure"):
                always_fail()

        assert call_count == MAX_RETRIES

    def test_timeout_exception_triggers_retry(self):
        """requests.Timeout should be caught and retried."""
        call_count = 0

        @retry_request
        def timeout_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.Timeout("timed out")
            return "ok"

        with patch("backend.app_service.time.sleep"):
            result = timeout_then_ok()
        assert result == "ok"
        assert call_count == 2

    def test_connection_error_triggers_retry(self):
        """ConnectionError should be caught and retried."""
        call_count = 0

        @retry_request
        def conn_err_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("connection reset")
            return "ok"

        with patch("backend.app_service.time.sleep"):
            result = conn_err_then_ok()
        assert result == "ok"
        assert call_count == 2

    def test_non_request_exception_not_retried(self):
        """Non-network exceptions should propagate immediately."""
        call_count = 0

        @retry_request
        def raise_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not a network error")

        with pytest.raises(ValueError, match="not a network error"):
            raise_value_error()
        assert call_count == 1


# ======================================================================
# AppService.__init__ Tests
# ======================================================================


class TestAppServiceInit:
    """Tests for AppService.__init__()."""

    def test_config_initialized_to_empty_dict(self):
        """config should be an empty dict on init."""
        svc = AppService()
        assert svc.config == {}

    def test_db_initialized_to_none(self):
        """db should be None on init."""
        svc = AppService()
        assert svc.db is None

    def test_plex_manager_initialized_to_none(self):
        """plex_manager should be None on init."""
        svc = AppService()
        assert svc.plex_manager is None

    def test_tmdb_cache_initialized(self):
        """tmdb_cache should be an LRUCache with maxsize 2000."""
        svc = AppService()
        assert isinstance(svc.tmdb_cache, LRUCache)
        assert svc.tmdb_cache.maxsize == 2000

    def test_omdb_cache_initialized(self):
        """omdb_cache should be an LRUCache with maxsize 2000."""
        svc = AppService()
        assert isinstance(svc.omdb_cache, LRUCache)
        assert svc.omdb_cache.maxsize == 2000

    def test_log_callback_initialized_to_none(self):
        """_log_callback should be None on init."""
        svc = AppService()
        assert svc._log_callback is None

    def test_shutdown_hooks_initialized_empty(self):
        """_shutdown_hooks should be an empty list on init."""
        svc = AppService()
        assert svc._shutdown_hooks == []

    def test_notification_manager_none(self):
        """notification_manager should be None before startup."""
        svc = AppService()
        assert svc.notification_manager is None

    def test_watchlist_manager_none(self):
        """watchlist_manager should be None before startup."""
        svc = AppService()
        assert svc.watchlist_manager is None

    def test_stats_dashboard_none(self):
        """stats_dashboard should be None before startup."""
        svc = AppService()
        assert svc.stats_dashboard is None

    def test_scheduler_thread_none(self):
        """_scheduler_thread should be None on init."""
        svc = AppService()
        assert svc._scheduler_thread is None

    def test_scheduler_stop_event(self):
        """_scheduler_stop should be a threading.Event."""
        svc = AppService()
        assert isinstance(svc._scheduler_stop, threading.Event)

    def test_scan_trigger_none(self):
        """_scan_trigger should be None on init."""
        svc = AppService()
        assert svc._scan_trigger is None


# ======================================================================
# AppService.startup Tests
# ======================================================================


class TestAppServiceStartup:
    """Tests for AppService.startup()."""

    def test_startup_returns_warnings_list(self):
        """startup() should return a list (possibly empty)."""
        svc = AppService()
        with patch.object(svc, "load_config", return_value=get_default_config()), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager") as mock_db_cls, \
             patch("backend.app_service.PlexManager") as mock_plex_cls, \
             patch.object(svc, "_init_optional_subsystems"):
            mock_db_cls.return_value = MagicMock()
            mock_plex_cls.return_value = MagicMock()
            warnings = svc.startup()
        assert isinstance(warnings, list)

    def test_startup_initializes_config(self):
        """After startup, config should be populated."""
        svc = AppService()
        default_cfg = get_default_config()
        with patch.object(svc, "load_config", return_value=default_cfg), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager") as mock_db_cls, \
             patch("backend.app_service.PlexManager") as mock_plex_cls, \
             patch.object(svc, "_init_optional_subsystems"):
            mock_db_cls.return_value = MagicMock()
            mock_plex_cls.return_value = MagicMock()
            svc.startup()
        assert svc.config == default_cfg

    def test_startup_initializes_db(self):
        """After startup, db should be set."""
        svc = AppService()
        mock_db = MagicMock()
        with patch.object(svc, "load_config", return_value=get_default_config()), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager", return_value=mock_db), \
             patch("backend.app_service.PlexManager") as mock_plex_cls, \
             patch.object(svc, "_init_optional_subsystems"):
            mock_plex_cls.return_value = MagicMock()
            svc.startup()
        assert svc.db is mock_db

    def test_startup_initializes_plex_manager(self):
        """After startup, plex_manager should be set."""
        svc = AppService()
        mock_plex = MagicMock()
        with patch.object(svc, "load_config", return_value=get_default_config()), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager") as mock_db_cls, \
             patch("backend.app_service.PlexManager", return_value=mock_plex), \
             patch.object(svc, "_init_optional_subsystems"):
            mock_db_cls.return_value = MagicMock()
            svc.startup()
        assert svc.plex_manager is mock_plex

    def test_startup_config_failure_uses_defaults(self):
        """If config loading fails, startup should use defaults and add a warning."""
        svc = AppService()
        with patch.object(svc, "load_config", side_effect=RuntimeError("config broke")), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager") as mock_db_cls, \
             patch("backend.app_service.PlexManager") as mock_plex_cls, \
             patch.object(svc, "_init_optional_subsystems"):
            mock_db_cls.return_value = MagicMock()
            mock_plex_cls.return_value = MagicMock()
            warnings = svc.startup()
        assert any("Config load failed" in w for w in warnings)
        # config should still be populated with defaults
        assert svc.config is not None
        assert len(svc.config) > 0

    def test_startup_db_failure_adds_warning(self):
        """If DatabaseManager init fails, a warning is added but startup continues."""
        svc = AppService()
        with patch.object(svc, "load_config", return_value=get_default_config()), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager", side_effect=RuntimeError("db broke")), \
             patch("backend.app_service.PlexManager") as mock_plex_cls, \
             patch.object(svc, "_init_optional_subsystems"):
            mock_plex_cls.return_value = MagicMock()
            warnings = svc.startup()
        assert any("Database init failed" in w for w in warnings)

    def test_startup_migrates_legacy_history_and_logs_recovered_count(self, tmp_path):
        svc = AppService()
        db = DatabaseManager(db_path=str(tmp_path / "startup_migration.db"))
        legacy_history = tmp_path / "download_history.json"
        legacy_history.write_text(
            json.dumps(["https://example.com/1", "https://example.com/2"]),
            encoding="utf-8",
        )

        with patch.object(svc, "load_config", return_value=get_default_config()), \
             patch("backend.app_service.setup_logging", return_value=logging.getLogger()), \
             patch("backend.app_service.DatabaseManager", return_value=db), \
             patch("backend.app_service.PlexManager", return_value=MagicMock()), \
             patch.object(svc, "_init_optional_subsystems"), \
             patch.object(
                 svc,
                 "_legacy_persistence_candidates",
                 return_value=[(str(legacy_history), str(tmp_path / "cache.json"))],
             ), \
             patch("backend.app_service.logger") as mock_logger:
            warnings = svc.startup()

        assert warnings == []
        assert db.get_history_count() == 2
        assert os.path.exists(str(legacy_history) + ".bak")
        assert any(
            call.args and "Recovered %d download history entries from database." in call.args[0]
            for call in mock_logger.info.call_args_list
        )
        db.close()

    def test_legacy_persistence_candidates_include_old_mediascout_data_dir(self, monkeypatch, tmp_path):
        svc = AppService()
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(app_service_module.os, "name", "nt", raising=False)

        candidates = svc._legacy_persistence_candidates()

        assert (
            str(tmp_path / "MediaScout" / "download_history.json"),
            str(tmp_path / "MediaScout" / "cache.json"),
        ) in candidates


# ======================================================================
# AppService.shutdown — Extended Tests
# ======================================================================


class TestAppServiceShutdownExtended:
    """Extended tests for AppService.shutdown()."""

    def _make_service(self):
        """Create a minimal AppService without real startup."""
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = None
        return svc

    def test_shutdown_calls_hooks_in_order(self):
        """Shutdown hooks should be called in registration order."""
        svc = self._make_service()
        order = []
        svc._shutdown_hooks = [
            lambda: order.append("first"),
            lambda: order.append("second"),
            lambda: order.append("third"),
        ]
        svc.shutdown()
        assert order == ["first", "second", "third"]

    def test_shutdown_closes_watchlist(self):
        """shutdown should close the watchlist_manager."""
        svc = self._make_service()
        svc.watchlist_manager = MagicMock()
        svc.shutdown()
        svc.watchlist_manager.close.assert_called_once()

    def test_shutdown_closes_db(self):
        """shutdown should close the database."""
        svc = self._make_service()
        svc.db = MagicMock()
        svc.shutdown()
        svc.db.close.assert_called_once()

    def test_shutdown_hook_error_does_not_prevent_db_close(self):
        """Even if a hook raises, db.close should still be called."""
        svc = self._make_service()
        svc.db = MagicMock()
        svc._shutdown_hooks = [MagicMock(side_effect=RuntimeError("boom"))]
        svc.shutdown()
        svc.db.close.assert_called_once()

    def test_shutdown_sets_scheduler_stop(self):
        """shutdown should signal the scheduler to stop."""
        svc = self._make_service()
        svc.shutdown()
        assert svc._scheduler_stop.is_set()

    def test_shutdown_no_watchlist_no_error(self):
        """shutdown with watchlist_manager=None should not error."""
        svc = self._make_service()
        svc.watchlist_manager = None
        svc.shutdown()  # Should not raise

    def test_shutdown_no_db_no_error(self):
        """shutdown with db=None should not error."""
        svc = self._make_service()
        svc.db = None
        svc.shutdown()  # Should not raise


# ======================================================================
# AppService.add_shutdown_hook Tests
# ======================================================================


class TestAddShutdownHook:
    """Tests for AppService.add_shutdown_hook()."""

    def test_hook_added_to_list(self):
        """add_shutdown_hook should append to _shutdown_hooks."""
        svc = AppService()
        fn = MagicMock()
        svc.add_shutdown_hook(fn)
        assert fn in svc._shutdown_hooks

    def test_multiple_hooks(self):
        """Multiple hooks should all be stored."""
        svc = AppService()
        fn1 = MagicMock()
        fn2 = MagicMock()
        svc.add_shutdown_hook(fn1)
        svc.add_shutdown_hook(fn2)
        assert len(svc._shutdown_hooks) == 2
        assert fn1 in svc._shutdown_hooks
        assert fn2 in svc._shutdown_hooks


# ======================================================================
# AppService.set_log_callback / log Tests
# ======================================================================


class TestLogCallback:
    """Tests for set_log_callback and log methods."""

    def _make_service(self):
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._log_callback = None
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = None
        return svc

    def test_set_log_callback(self):
        """set_log_callback should store the callback."""
        svc = self._make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        assert svc._log_callback is cb

    def test_log_invokes_callback(self):
        """log() should invoke the registered callback with message and level."""
        svc = self._make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.log("test message", "info")
        cb.assert_called_once_with("test message", "info")

    def test_log_with_warning_level(self):
        """log() with level='warning' should forward that level to callback."""
        svc = self._make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.log("warn msg", "warning")
        cb.assert_called_once_with("warn msg", "warning")

    def test_log_without_callback_does_not_error(self):
        """log() without a callback should not raise."""
        svc = self._make_service()
        svc.log("no callback set", "info")  # Should not raise

    def test_log_callback_exception_swallowed(self):
        """If the callback raises, it should be swallowed."""
        svc = self._make_service()
        cb = MagicMock(side_effect=RuntimeError("callback error"))
        svc.set_log_callback(cb)
        svc.log("test", "info")  # Should not raise

    def test_log_success_level_uses_info(self):
        """log() with level='success' should map to logger.info internally."""
        svc = self._make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.log("success msg", "success")
        cb.assert_called_once_with("success msg", "success")

    def test_log_default_level_is_info(self):
        """log() without an explicit level should default to 'info'."""
        svc = self._make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc.log("default level")
        cb.assert_called_once_with("default level", "info")


# ======================================================================
# AppService.save_config Tests
# ======================================================================


class TestSaveConfig:
    """Tests for AppService.save_config()."""

    def _make_service(self, config=None):
        svc = AppService.__new__(AppService)
        svc.config = config or get_default_config()
        svc._log_callback = None
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc._config_lock = threading.RLock()
        svc.watchlist_manager = None
        svc.db = None
        return svc

    def test_save_config_writes_json(self, tmp_path):
        """save_config should write config as JSON to CONFIG_FILE."""
        config_file = tmp_path / "config.json"
        svc = self._make_service({"key": "value", "number": 42})
        with patch("backend.app_service.CONFIG_FILE", str(config_file)):
            svc.save_config()
        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["key"] == "value"
        assert data["number"] == 42

    def test_save_config_atomic_replace(self, tmp_path):
        """save_config should use atomic write (write tmp then replace)."""
        config_file = tmp_path / "config.json"
        # Write initial content
        config_file.write_text('{"old": true}')
        svc = self._make_service({"new": True})
        with patch("backend.app_service.CONFIG_FILE", str(config_file)):
            svc.save_config()
        data = json.loads(config_file.read_text())
        assert data["new"] is True
        assert "old" not in data

    def test_save_config_no_tmp_file_left(self, tmp_path):
        """After save_config, no .tmp file should remain."""
        config_file = tmp_path / "config.json"
        svc = self._make_service({"test": True})
        with patch("backend.app_service.CONFIG_FILE", str(config_file)):
            svc.save_config()
        tmp_file = tmp_path / "config.json.tmp"
        assert not tmp_file.exists()

    def test_save_config_roundtrip(self, tmp_path):
        """Saved config should be loadable and match the original."""
        config_file = tmp_path / "config.json"
        original = get_default_config()
        svc = self._make_service(original)
        with patch("backend.app_service.CONFIG_FILE", str(config_file)):
            svc.save_config()
        loaded = json.loads(config_file.read_text())
        for key, value in original.items():
            assert loaded[key] == value, f"Mismatch for key '{key}'"


# ======================================================================
# AppService.validate_config_values — Extended Tests
# ======================================================================


class TestValidateConfigValuesExtended:
    """Extended tests for AppService.validate_config_values()."""

    def _make_service(self, config_overrides=None):
        svc = AppService.__new__(AppService)
        svc.config = get_default_config()
        if config_overrides:
            svc.config.update(config_overrides)
        return svc

    def test_numeric_out_of_range_high_corrected(self):
        """A numeric value above the max should be reset to default."""
        svc = self._make_service({"scan_threads": 999})
        result = svc.validate_config_values()
        assert any("scan_threads" in w for w in result["warnings"])
        assert svc.config["scan_threads"] == 10  # default for scan_threads

    def test_numeric_out_of_range_low_corrected(self):
        """A numeric value below the min should be reset to default."""
        svc = self._make_service({"cache_duration": -10})
        result = svc.validate_config_values()
        assert any("cache_duration" in w for w in result["warnings"])
        assert svc.config["cache_duration"] == 4  # default

    def test_non_numeric_string_corrected(self):
        """A string where a number is expected should be corrected to default."""
        svc = self._make_service({"upgrade_sensitivity": "abc"})
        result = svc.validate_config_values()
        assert any("upgrade_sensitivity" in w for w in result["warnings"])
        assert svc.config["upgrade_sensitivity"] == 2  # default

    def test_boolean_int_conversion(self):
        """Integer 1 for a boolean field should be converted to True with warning."""
        svc = self._make_service({"debug_mode": 1})
        result = svc.validate_config_values()
        assert any("debug_mode" in w for w in result["warnings"])
        assert svc.config["debug_mode"] is True

    def test_boolean_zero_becomes_false(self):
        """Integer 0 for a boolean field should become False."""
        svc = self._make_service({"use_tmdb": 0})
        result = svc.validate_config_values()
        assert svc.config["use_tmdb"] is False

    def test_boolean_string_conversion(self):
        """A non-empty string for a boolean field should become True."""
        svc = self._make_service({"jd_enabled": "yes"})
        result = svc.validate_config_values()
        assert any("jd_enabled" in w for w in result["warnings"])
        assert svc.config["jd_enabled"] is True

    def test_plex_url_without_protocol_warns(self):
        """A Plex URL without http/https should produce a warning."""
        svc = self._make_service({"plex_url": "192.168.1.100:32400"})
        result = svc.validate_config_values()
        assert any("Plex URL" in w for w in result["warnings"])

    def test_plex_url_with_http_ok(self):
        """A Plex URL with http:// should not produce a URL warning."""
        svc = self._make_service({"plex_url": "http://192.168.1.100:32400"})
        result = svc.validate_config_values()
        plex_warnings = [w for w in result["warnings"] if "Plex URL" in w]
        assert len(plex_warnings) == 0

    def test_plex_url_with_https_ok(self):
        """A Plex URL with https:// should not produce a URL warning."""
        svc = self._make_service({"plex_url": "https://plex.example.com"})
        result = svc.validate_config_values()
        plex_warnings = [w for w in result["warnings"] if "Plex URL" in w]
        assert len(plex_warnings) == 0

    def test_plex_url_empty_no_warning(self):
        """An empty plex_url should not generate a URL warning."""
        svc = self._make_service({"plex_url": ""})
        result = svc.validate_config_values()
        plex_warnings = [w for w in result["warnings"] if "Plex URL" in w]
        assert len(plex_warnings) == 0

    def test_valid_defaults_no_warnings(self):
        """Default config should produce no warnings or errors."""
        svc = self._make_service()
        result = svc.validate_config_values()
        assert result["warnings"] == []
        assert result["errors"] == []

    def test_scheduler_interval_below_min(self):
        """scheduler_interval below minimum should be corrected."""
        svc = self._make_service({"scheduler_interval": 0})
        result = svc.validate_config_values()
        assert any("scheduler_interval" in w for w in result["warnings"])
        assert svc.config["scheduler_interval"] == 24  # default

    def test_match_thresholds_out_of_range(self):
        """Match thresholds above 100 should be corrected."""
        svc = self._make_service({
            "tv_match_threshold": 200,
            "movie_match_threshold": -5,
        })
        result = svc.validate_config_values()
        assert any("tv_match_threshold" in w for w in result["warnings"])
        assert any("movie_match_threshold" in w for w in result["warnings"])


# ======================================================================
# AppService.apply_preset — Extended Tests
# ======================================================================


class TestApplyPresetExtended:
    """Extended tests for AppService.apply_preset()."""

    def _make_service(self):
        svc = AppService.__new__(AppService)
        svc.config = get_default_config()
        svc.save_config = MagicMock()
        return svc

    def test_valid_preset_returns_true(self):
        """Applying a valid preset should return True."""
        svc = self._make_service()
        assert svc.apply_preset("Aggressive Upgrades") is True

    def test_valid_preset_updates_config(self):
        """Applying a valid preset should update config values."""
        svc = self._make_service()
        svc.apply_preset("Conservative")
        preset = SETTINGS_PRESETS["Conservative"]
        for key, value in preset.items():
            if key != "description":
                assert svc.config[key] == value

    def test_valid_preset_calls_save(self):
        """Applying a valid preset should call save_config."""
        svc = self._make_service()
        svc.apply_preset("Balanced")
        svc.save_config.assert_called_once()

    def test_invalid_preset_returns_false(self):
        """Applying a nonexistent preset should return False."""
        svc = self._make_service()
        assert svc.apply_preset("Nonexistent Preset") is False

    def test_invalid_preset_does_not_save(self):
        """An invalid preset should not trigger save_config."""
        svc = self._make_service()
        svc.apply_preset("Nonexistent Preset")
        svc.save_config.assert_not_called()

    def test_invalid_preset_does_not_modify_config(self):
        """An invalid preset should leave config unchanged."""
        svc = self._make_service()
        original_config = dict(svc.config)
        svc.apply_preset("Nonexistent Preset")
        assert svc.config == original_config

    def test_preset_does_not_set_description_key(self):
        """The 'description' key from the preset should not be placed in config."""
        svc = self._make_service()
        svc.apply_preset("4K Only")
        # description should not be in config, or if it was already there, not overwritten
        assert svc.config.get("description") != SETTINGS_PRESETS["4K Only"]["description"]

    def test_all_presets_applicable(self):
        """Every preset in SETTINGS_PRESETS should be applicable."""
        for name in SETTINGS_PRESETS:
            svc = self._make_service()
            assert svc.apply_preset(name) is True


# ======================================================================
# AppService.load_download_history Tests
# ======================================================================


class TestLoadDownloadHistory:
    """Tests for AppService.load_download_history()."""

    def _make_service(self):
        svc = AppService.__new__(AppService)
        svc.config = {}
        svc._log_callback = None
        svc._shutdown_hooks = []
        svc._scheduler_stop = threading.Event()
        svc.watchlist_manager = None
        svc.db = MagicMock()
        return svc

    def test_returns_set_of_urls(self):
        """load_download_history should return a set of URL strings."""
        svc = self._make_service()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("https://example.com/1",),
            ("https://example.com/2",),
        ]
        svc.db.transaction.return_value.__enter__ = MagicMock(return_value=mock_conn)
        svc.db.transaction.return_value.__exit__ = MagicMock(return_value=False)
        result = svc.load_download_history()
        assert isinstance(result, set)
        assert result == {"https://example.com/1", "https://example.com/2"}

    def test_empty_database_returns_empty_set(self):
        """With no rows, load_download_history should return an empty set."""
        svc = self._make_service()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        svc.db.transaction.return_value.__enter__ = MagicMock(return_value=mock_conn)
        svc.db.transaction.return_value.__exit__ = MagicMock(return_value=False)
        result = svc.load_download_history()
        assert result == set()

    def test_db_error_returns_empty_set(self):
        """If the database raises, load_download_history should return an empty set."""
        svc = self._make_service()
        svc.db.transaction.return_value.__enter__ = MagicMock(
            side_effect=RuntimeError("db error")
        )
        svc.db.transaction.return_value.__exit__ = MagicMock(return_value=False)
        result = svc.load_download_history()
        assert result == set()

    def test_conn_none_returns_empty_set(self):
        """If transaction yields None, should return empty set."""
        svc = self._make_service()
        svc.db.transaction.return_value.__enter__ = MagicMock(return_value=None)
        svc.db.transaction.return_value.__exit__ = MagicMock(return_value=False)
        result = svc.load_download_history()
        assert result == set()


# ======================================================================
# AppService.set_scan_trigger Tests
# ======================================================================


class TestSetScanTrigger:
    """Tests for AppService.set_scan_trigger()."""

    def test_stores_callback(self):
        """set_scan_trigger should store the callback in _scan_trigger."""
        svc = AppService()
        cb = MagicMock()
        svc.set_scan_trigger(cb)
        assert svc._scan_trigger is cb

    def test_stores_none(self):
        """set_scan_trigger(None) should clear the trigger."""
        svc = AppService()
        svc.set_scan_trigger(MagicMock())
        svc.set_scan_trigger(None)
        assert svc._scan_trigger is None

    def test_replaces_existing_callback(self):
        """Setting a new callback should replace the old one."""
        svc = AppService()
        cb1 = MagicMock()
        cb2 = MagicMock()
        svc.set_scan_trigger(cb1)
        svc.set_scan_trigger(cb2)
        assert svc._scan_trigger is cb2
        assert svc._scan_trigger is not cb1
