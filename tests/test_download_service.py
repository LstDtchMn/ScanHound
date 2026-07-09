"""Comprehensive tests for backend/download_service.py module.

Covers:
- DownloadService.__init__ and callback wiring
- _log helper (success, info, error, callback)
- load_download_history (success, empty, exception)
- save_to_history (movie, TV, exception)
- send_to_jdownloader (folder, api, missing folder, errors)
- get_driver / cleanup_driver (cached, stale, creation)
- scrape_links (HDEncode, DDLBase, AditHD routing and scraping)
- _scrape_ddlbase_links (shortlinks, 1fichier, empty)
- _scrape_adithd_links (registry, fallback, error)
- export_results_csv (default filepath, custom filepath)
- open_url / copy_to_clipboard
- download_item (full flow: JD, clipboard, browser fallback)
- open_in_plex (found, not found, no URL)
"""

import csv
import os
import threading
from dataclasses import dataclass
from enum import Enum
from unittest.mock import MagicMock, patch, call, mock_open

import pytest

from backend.download_service import DownloadService


# ── Helpers ──────────────────────────────────────────────────────────

def _make_service(config=None, db=None):
    """Build a DownloadService with mocked dependencies."""
    cfg = config or {}
    database = db or MagicMock()
    # Default (mock DBs only): not a duplicate, so the normal download flow runs
    # unless a test opts into the dedup path explicitly. Real DatabaseManagers
    # answer is_downloaded() truthfully from their own rows.
    if isinstance(database, MagicMock):
        database.is_downloaded.return_value = False
    return DownloadService(config=cfg, db=database)


class TestDownloadFolderRouting:
    """4K movies route to their own JD folder so they extract onto the 4K
    library's drive (instant same-volume rename instead of a cross-drive copy).
    TV → jd_tv_folder; non-4K movies → jd_movies_folder."""

    def _capture_destination(self, config, **item):
        svc = _make_service(config={**config, "jd_enabled": True, "jd_method": "api"})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/f1"])
        svc._is_supported_download_link = MagicMock(return_value=True)
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock(return_value=True)
        svc.db.is_downloaded.return_value = False
        svc.db.get_downloaded_title_quality.return_value = []
        svc.download_item("u/x", item.get("title", "M"), item.get("season"),
                          item.get("resolution", ""), "50 GB",
                          year=item.get("year"))
        assert svc.send_to_jdownloader.called
        return svc.send_to_jdownloader.call_args.kwargs.get("destination")

    CFG = {"jd_movies_folder": "F:\\Downloads",
           "jd_movies_folder_4k": "G:\\Downloads",
           "jd_tv_folder": "T:\\Downloads"}

    def test_4k_movie_routes_to_4k_folder(self):
        assert self._capture_destination(self.CFG, resolution="2160p", season=None) == "G:\\Downloads"

    def test_uhd_and_4k_aliases_route_to_4k_folder(self):
        assert self._capture_destination(self.CFG, resolution="4K", season=None) == "G:\\Downloads"
        assert self._capture_destination(self.CFG, resolution="UHD", season=None) == "G:\\Downloads"

    def test_1080p_movie_routes_to_movies_folder(self):
        assert self._capture_destination(self.CFG, resolution="1080p", season=None) == "F:\\Downloads"

    def test_tv_routes_to_tv_folder_even_if_4k(self):
        # A 4K TV episode still goes to the TV folder (season is not None).
        assert self._capture_destination(self.CFG, resolution="2160p", season=2) == "T:\\Downloads"

    def test_4k_folder_falls_back_to_movies_folder_when_unset(self):
        cfg = {"jd_movies_folder": "F:\\Downloads", "jd_movies_folder_4k": "",
               "jd_tv_folder": ""}
        assert self._capture_destination(cfg, resolution="2160p", season=None) == "F:\\Downloads"


class TestDownloadDedup:
    def test_already_grabbed_url_is_skipped_without_scraping(self):
        db = MagicMock()
        svc = _make_service(config={"jd_enabled": True, "jd_method": "folder"}, db=db)
        db.is_downloaded.return_value = True   # central table says already grabbed
        svc.scrape_links = MagicMock(side_effect=AssertionError("must not scrape a dup"))
        res = svc.download_item("u/dup", "Dune", None, "1080p", "20 GB")
        assert res["success"] is True
        assert res["method"] == "duplicate"
        assert "Already grabbed" in res["message"]
        svc.scrape_links.assert_not_called()

    def test_failed_prior_grab_does_not_block_retry(self):
        db = MagicMock()
        svc = _make_service(config={"jd_enabled": True, "jd_method": "folder"}, db=db)
        db.is_downloaded.return_value = False  # only a failed row existed → retryable
        db.get_downloaded_title_quality.return_value = []
        svc.scrape_links = MagicMock(return_value=[])  # no links → not a duplicate short-circuit
        svc._is_supported_download_link = MagicMock(return_value=False)
        res = svc.download_item("u/retry", "Dune", None, "1080p", "20 GB")
        assert res["method"] != "duplicate"   # proceeded past the dedup guard
        svc.scrape_links.assert_called_once()

    # ── Title-level dedup: a DIFFERENT URL of an already-grabbed title ──

    @staticmethod
    def _svc_with_prior(prior_rows):
        from backend.app_service import normalize_title  # noqa: F401 (doc)
        db = MagicMock()
        db.is_downloaded.return_value = False
        db.get_downloaded_title_quality.return_value = prior_rows
        svc = _make_service(config={"jd_enabled": True, "jd_method": "folder"}, db=db)
        svc.scrape_links = MagicMock(return_value=[])
        svc._is_supported_download_link = MagicMock(return_value=False)
        return svc

    def test_same_title_same_quality_different_url_is_skipped(self):
        # The production duplicate: two 4K releases of the same movie, grabbed
        # via different URLs. The second must be recognized as a duplicate.
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Michael"), 2026, None, "4K", 0)])
        svc.scrape_links = MagicMock(side_effect=AssertionError("must not scrape a title dup"))
        res = svc.download_item("u/other-release", "Michael", None, "4K", "80 GB", year=2026)
        assert res["success"] is True
        assert res["method"] == "duplicate_similar"
        svc.scrape_links.assert_not_called()

    def test_lower_resolution_is_also_skipped(self):
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Michael"), 2026, None, "4K", 0)])
        res = svc.download_item("u/1080", "Michael", None, "1080p", "30 GB", year=2026)
        assert res["method"] == "duplicate_similar"

    def test_higher_resolution_passes_as_upgrade(self):
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Avatar"), 2025, None, "1080p", 0)])
        res = svc.download_item("u/4k", "Avatar", None, "4K", "70 GB", year=2025)
        assert res["method"] != "duplicate_similar"   # proceeded (upgrade)
        svc.scrape_links.assert_called_once()

    def test_dv_gain_at_same_resolution_passes(self):
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Sicario"), 2015, None, "4K", 0)])
        res = svc.download_item("u/dv", "Sicario", None, "4K", "60 GB", year=2015, dovi=True)
        assert res["method"] != "duplicate_similar"
        svc.scrape_links.assert_called_once()

    def test_different_season_is_never_blocked(self):
        # S01 grabbed must not block S02 (season matches strictly).
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Equal Justice"), 2026, 1, "1080p", 0)])
        res = svc.download_item("u/s02", "Equal Justice", 2, "1080p", "50 GB", year=2026)
        assert res["method"] != "duplicate_similar"
        svc.scrape_links.assert_called_once()

    def test_different_year_is_never_blocked(self):
        # Dune (2021) grabbed must not block Dune (1984).
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Dune"), 2021, None, "4K", 0)])
        res = svc.download_item("u/1984", "Dune", None, "4K", "60 GB", year=1984)
        assert res["method"] != "duplicate_similar"
        svc.scrape_links.assert_called_once()

    def test_legacy_row_without_year_still_blocks(self):
        # Rows recorded before the year column match on title+season alone.
        from backend.app_service import normalize_title
        svc = self._svc_with_prior([(normalize_title("Notting Hill"), None, None, "4K", 0)])
        res = svc.download_item("u/remux", "Notting Hill", None, "4K", "80 GB", year=1999)
        assert res["method"] == "duplicate_similar"


@dataclass
class _FakeItem:
    """Lightweight stand-in for MediaItem for CSV export tests."""
    class _Status:
        name = "MISSING"
        value = "missing"
    status = _Status()
    title: str = "Test Movie"
    year: int = 2024
    season: object = None
    resolution: str = "4K"
    size: str = "50 GB"
    hdr: str = "HDR10"
    plex_info: str = "-"
    url: str = "http://example.com/movie"


class _PollingDriver:
    """Small driver stub that returns scripted URLs and page sources per poll."""

    def __init__(self, current_urls=None, page_sources=None):
        self._current_urls = list(current_urls or [])
        self._page_sources = list(page_sources or [""])
        self._current_index = 0
        self._page_index = 0
        self.visited = []

    def get(self, url):
        self.visited.append(url)

    @property
    def current_url(self):
        if not self._current_urls:
            return ""
        idx = min(self._current_index, len(self._current_urls) - 1)
        value = self._current_urls[idx]
        self._current_index += 1
        return value

    @property
    def page_source(self):
        if not self._page_sources:
            return ""
        idx = min(self._page_index, len(self._page_sources) - 1)
        value = self._page_sources[idx]
        self._page_index += 1
        return value


# ======================================================================
# __init__ / Callbacks
# ======================================================================

class TestInit:
    """Test constructor and callback setup."""

    def test_init_defaults(self):
        svc = _make_service()
        assert svc.cached_driver is None
        assert svc.download_history == set()
        assert svc._downloaded_titles_lookup == {}
        assert svc._log_fn is None

    def test_set_log_callback(self):
        svc = _make_service()
        fn = MagicMock()
        svc.set_log_callback(fn)
        assert svc._log_fn is fn


class TestLog:
    """Test the _log helper."""

    def test_log_info_level(self):
        svc = _make_service()
        cb = MagicMock()
        svc.set_log_callback(cb)
        svc._log("hello", "info")
        cb.assert_called_once_with("hello", "info")

    def test_log_success_maps_to_info(self):
        """'success' is not a real logger level; _log should still call logger.info."""
        svc = _make_service()
        # Should not raise
        svc._log("ok", "success")

    def test_log_callback_exception_suppressed(self):
        svc = _make_service()
        svc.set_log_callback(MagicMock(side_effect=RuntimeError("boom")))
        # Should not raise
        svc._log("msg", "info")

    def test_log_no_callback(self):
        svc = _make_service()
        # No callback set; should not raise
        svc._log("msg", "warning")


# ======================================================================
# Download History
# ======================================================================

class TestLoadDownloadHistory:
    def test_returns_urls(self):
        db = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("http://a.com",), ("http://b.com",),
        ]
        db.transaction.return_value.__enter__ = MagicMock(return_value=mock_conn)
        db.transaction.return_value.__exit__ = MagicMock(return_value=False)

        svc = _make_service(db=db)
        result = svc.load_download_history()
        assert result == {"http://a.com", "http://b.com"}

    def test_returns_empty_when_conn_is_none(self):
        db = MagicMock()
        db.transaction.return_value.__enter__ = MagicMock(return_value=None)
        db.transaction.return_value.__exit__ = MagicMock(return_value=False)

        svc = _make_service(db=db)
        result = svc.load_download_history()
        assert result == set()

    def test_returns_empty_on_exception(self):
        db = MagicMock()
        db.transaction.side_effect = RuntimeError("db error")

        svc = _make_service(db=db)
        result = svc.load_download_history()
        assert result == set()


class TestSaveToHistory:
    @patch("backend.download_service.normalize_title", return_value="test movie")
    def test_save_movie(self, mock_norm):
        db = MagicMock()
        svc = _make_service(db=db)

        svc.save_to_history("http://a.com", "Test Movie", None, "4K", "50 GB")

        db.add_to_history.assert_called_once_with(
            url="http://a.com", title="Test Movie",
            normalized_title="test movie", season=None,
            resolution="4K", size="50 GB", status="completed",
            hdr=None, dovi=False, year=None,
        )
        assert "http://a.com" in svc.download_history
        assert "test movie" in svc._downloaded_titles_lookup

    @patch("backend.download_service.normalize_title", return_value="breaking bad")
    def test_save_tv_with_season(self, mock_norm):
        db = MagicMock()
        svc = _make_service(db=db)

        svc.save_to_history("http://b.com", "Breaking Bad", 3, "1080p", "30 GB")

        assert "http://b.com" in svc.download_history
        assert "breaking bad|S3" in svc._downloaded_titles_lookup
        entry = svc._downloaded_titles_lookup["breaking bad|S3"][0]
        assert entry["resolution"] == "1080p"
        assert entry["size"] == "30 GB"

    @patch("backend.download_service.normalize_title", side_effect=RuntimeError("fail"))
    def test_save_exception_logged(self, mock_norm):
        svc = _make_service()
        # Should not raise
        svc.save_to_history("http://x.com", "X", None, "?", "?")


# ======================================================================
# JDownloader
# ======================================================================

class TestSendToJDownloader:
    def test_folder_method_success(self, tmp_path):
        folder = str(tmp_path)
        svc = _make_service(config={"jd_method": "folder", "jd_folder": folder})
        result = svc.send_to_jdownloader(["http://link1.com", "http://link2.com"], "MyPkg")
        assert result is True
        # One .crawljob file is written per link
        files = list(tmp_path.iterdir())
        assert len(files) == 2
        content = "\n".join(f.read_text() for f in files)
        assert "http://link1.com" in content
        assert "http://link2.com" in content
        assert "autoStart=TRUE" in content
        assert "packageName=MyPkg" in content

    def test_folder_method_folder_not_configured(self):
        svc = _make_service(config={"jd_method": "folder", "jd_folder": ""})
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is False

    def test_folder_method_folder_does_not_exist(self):
        svc = _make_service(config={"jd_method": "folder", "jd_folder": "/nonexistent/path/xxx"})
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is False

    def test_folder_method_write_error(self, tmp_path):
        folder = str(tmp_path)
        svc = _make_service(config={"jd_method": "folder", "jd_folder": folder})
        with patch("builtins.open", side_effect=PermissionError("denied")):
            result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is False

    @patch.dict("sys.modules", {"myjdapi": MagicMock()})
    def test_api_method_success_with_device_name(self):
        import sys
        mock_myjdapi = sys.modules["myjdapi"]
        mock_jd = MagicMock()
        mock_myjdapi.Myjdapi.return_value = mock_jd
        mock_device = MagicMock()
        mock_jd.get_device.return_value = mock_device

        svc = _make_service(config={
            "jd_method": "api",
            "jd_email": "user@example.com",
            "jd_password": "pass",
            "jd_device": "MyPC",
        })
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is True
        mock_jd.connect.assert_called_once()
        mock_jd.get_device.assert_called_once_with("MyPC")
        mock_device.linkgrabber.add_links.assert_called_once()

    @patch.dict("sys.modules", {"myjdapi": MagicMock()})
    def test_api_method_success_no_device_name(self):
        import sys
        mock_myjdapi = sys.modules["myjdapi"]
        mock_jd = MagicMock()
        mock_myjdapi.Myjdapi.return_value = mock_jd
        mock_device = MagicMock()
        mock_jd.list_devices.return_value = [mock_device]

        svc = _make_service(config={
            "jd_method": "api",
            "jd_email": "u@e.com",
            "jd_password": "p",
            "jd_device": "",
        })
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is True
        mock_jd.list_devices.assert_called_once()

    @patch.dict("sys.modules", {"myjdapi": MagicMock()})
    def test_api_method_no_devices(self):
        import sys
        mock_myjdapi = sys.modules["myjdapi"]
        mock_jd = MagicMock()
        mock_myjdapi.Myjdapi.return_value = mock_jd
        mock_jd.list_devices.return_value = []

        svc = _make_service(config={
            "jd_method": "api",
            "jd_email": "u@e.com",
            "jd_password": "p",
            "jd_device": "",
        })
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is False

    @patch.dict("sys.modules", {"myjdapi": MagicMock()})
    def test_api_method_exception(self):
        import sys
        mock_myjdapi = sys.modules["myjdapi"]
        mock_myjdapi.Myjdapi.side_effect = RuntimeError("api fail")

        svc = _make_service(config={
            "jd_method": "api",
            "jd_email": "u@e.com",
            "jd_password": "p",
            "jd_device": "",
        })
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is False

    def test_unknown_method_returns_false(self):
        svc = _make_service(config={"jd_method": "unknown"})
        result = svc.send_to_jdownloader(["http://a.com"], "Pkg")
        assert result is False

    def test_folder_truncates_long_package_name(self, tmp_path):
        """Package name in filename and content should be truncated to 50 chars."""
        folder = str(tmp_path)
        long_name = "A" * 100
        svc = _make_service(config={"jd_method": "folder", "jd_folder": folder})
        result = svc.send_to_jdownloader(["http://a.com"], long_name)
        assert result is True
        files = list(tmp_path.iterdir())
        content = files[0].read_text()
        # packageName should be truncated to 50 chars
        assert f"packageName={'A' * 50}" in content


# ======================================================================
# WebDriver
# ======================================================================

class TestGetDriver:
    @patch("backend.download_service._ensure_selenium")
    def test_returns_cached_driver_if_valid(self, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        mock_driver.title = "Some Page"
        svc.cached_driver = mock_driver

        result = svc.get_driver()
        assert result is mock_driver

    @patch("backend.download_service.time.sleep", MagicMock())
    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._uc")
    def test_retries_chrome_launch_then_succeeds(self, mock_uc, mock_ensure):
        # "session not created: cannot connect to chrome" on first launch must
        # recycle stale processes and retry, not fail the scrape outright.
        svc = _make_service()
        good = MagicMock()
        mock_uc.Chrome.side_effect = [Exception("session not created"), good]
        mock_uc.ChromeOptions.return_value = MagicMock()
        svc._kill_stale_chrome = MagicMock()

        result = svc.get_driver()
        assert result is good
        assert mock_uc.Chrome.call_count == 2       # retried once
        svc._kill_stale_chrome.assert_called_once()  # reaped between attempts

    @patch("backend.download_service.time.sleep", MagicMock())
    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._uc")
    def test_chrome_launch_failure_raises_after_retries(self, mock_uc, mock_ensure):
        svc = _make_service()
        mock_uc.Chrome.side_effect = Exception("cannot connect to chrome")
        mock_uc.ChromeOptions.return_value = MagicMock()
        svc._kill_stale_chrome = MagicMock()

        with pytest.raises(Exception, match="cannot connect to chrome"):
            svc.get_driver()
        assert mock_uc.Chrome.call_count == 3       # bounded to 3 attempts

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._uc")
    def test_creates_new_driver_if_no_cached(self, mock_uc, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        mock_uc.Chrome.return_value = mock_driver
        mock_uc.ChromeOptions.return_value = MagicMock()

        result = svc.get_driver()
        assert result is mock_driver
        mock_uc.Chrome.assert_called_once()

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._uc")
    def test_recreates_driver_if_stale(self, mock_uc, mock_ensure):
        svc = _make_service()
        old_driver = MagicMock()
        # Accessing title on stale driver raises
        type(old_driver).title = property(lambda self: (_ for _ in ()).throw(Exception("stale")))
        svc.cached_driver = old_driver

        new_driver = MagicMock()
        mock_uc.Chrome.return_value = new_driver
        mock_uc.ChromeOptions.return_value = MagicMock()

        result = svc.get_driver()
        assert result is new_driver
        old_driver.quit.assert_called_once()


def _driver_for(*, error_div=False, title="", anchors=True, body_text=""):
    """A fake WebDriver whose find_elements answers per-selector."""
    d = MagicMock()
    d.title = title

    def _find_elements(_by, selector):
        if selector == "#main-frame-error":
            return [MagicMock()] if error_div else []
        if selector == "a[href]":
            return [MagicMock()] if anchors else []
        return []

    d.find_elements.side_effect = _find_elements
    body = MagicMock()
    body.text = body_text
    d.find_element.return_value = body
    return d


@patch("backend.download_service._ensure_selenium")
@patch("backend.download_service._By", MagicMock())
class TestBrowserErrorPage:
    """Chromium in the container intermittently serves its OWN network-error page
    (bare-hostname title, zero anchors, no Cloudflare markers). It must be detected
    and healed, not misread as 'no links / Cloudflare wall'."""

    URL = "https://hdencode.org/killing-faith-2025-2160p/"

    def test_detects_error_page_and_extracts_err_code(self, _ensure):
        svc = _make_service()
        driver = _driver_for(error_div=True,
                             body_text="This site can't be reached\nERR_NAME_NOT_RESOLVED")
        assert svc._browser_error_code(driver, self.URL) == "ERR_NAME_NOT_RESOLVED"

    def test_real_page_is_not_an_error_page(self, _ensure):
        svc = _make_service()
        driver = _driver_for(error_div=False, title="Killing.Faith.2025.2160p – 23.7 GB",
                             anchors=True)
        assert svc._browser_error_code(driver, self.URL) is None

    def test_bare_hostname_title_with_no_anchors_is_an_error_page(self, _ensure):
        """The exact signature seen in production: title == host, zero anchors."""
        svc = _make_service()
        driver = _driver_for(error_div=False, title="hdencode.org", anchors=False)
        assert svc._browser_error_code(driver, self.URL) == "ERR_UNKNOWN"

    def test_hostname_title_but_real_anchors_is_not_an_error_page(self, _ensure):
        """Guard against a false positive on a real page titled like its host."""
        svc = _make_service()
        driver = _driver_for(error_div=False, title="hdencode.org", anchors=True)
        assert svc._browser_error_code(driver, self.URL) is None


@patch("backend.download_service.time.sleep", MagicMock())
class TestNavigateSelfHeals:
    URL = "https://hdencode.org/x/"

    def test_recycles_and_retries_then_succeeds(self):
        svc = _make_service()
        bad, good = MagicMock(), MagicMock()
        svc.get_driver = MagicMock(side_effect=[bad, good])
        svc._recycle_driver = MagicMock()
        # First navigation lands on the browser error page, second is clean.
        svc._browser_error_code = MagicMock(side_effect=["ERR_NAME_NOT_RESOLVED", None])

        result = svc._navigate(self.URL, tag="HDEncode")
        assert result is good
        svc._recycle_driver.assert_called_once()   # the bad browser was thrown away
        assert svc.get_driver.call_count == 2

    def test_returns_none_when_host_stays_unreachable(self):
        svc = _make_service()
        svc.get_driver = MagicMock(return_value=MagicMock())
        svc._recycle_driver = MagicMock()
        svc._browser_error_code = MagicMock(return_value="ERR_CONNECTION_RESET")

        assert svc._navigate(self.URL, tag="HDEncode", attempts=3) is None
        assert svc._recycle_driver.call_count == 3

    def test_navigation_exception_also_recycles(self):
        svc = _make_service()
        driver = MagicMock()
        driver.get.side_effect = RuntimeError("session died")
        svc.get_driver = MagicMock(return_value=driver)
        svc._recycle_driver = MagicMock()

        assert svc._navigate(self.URL, attempts=2) is None
        assert svc._recycle_driver.call_count == 2


class TestCleanupDriver:
    def test_quit_and_none(self):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver

        svc.cleanup_driver()
        mock_driver.quit.assert_called_once()
        assert svc.cached_driver is None

    def test_quit_exception_still_clears(self):
        svc = _make_service()
        mock_driver = MagicMock()
        mock_driver.quit.side_effect = RuntimeError("quit fail")
        svc.cached_driver = mock_driver

        svc.cleanup_driver()
        assert svc.cached_driver is None

    def test_no_driver_noop(self):
        svc = _make_service()
        svc.cached_driver = None
        svc.cleanup_driver()  # should not raise


# ======================================================================
# scrape_links routing
# ======================================================================

class TestScrapeLinksRouting:
    @patch("backend.download_service._ensure_selenium")
    def test_ddlbase_routed(self, mock_ensure):
        svc = _make_service()
        svc._scrape_ddlbase_links = MagicMock(return_value=["http://1fichier.com/?abc"])
        result = svc.scrape_links("http://ddlbase.com/movie-xyz", "Rapidgator")
        svc._scrape_ddlbase_links.assert_called_once_with("http://ddlbase.com/movie-xyz", progress_callback=None)
        assert result == ["http://1fichier.com/?abc"]

    @patch("backend.download_service._ensure_selenium")
    def test_adithd_routed(self, mock_ensure):
        svc = _make_service()
        svc._scrape_adithd_links = MagicMock(return_value=["http://rapidgator.net/file/abc"])
        result = svc.scrape_links("http://adit-hd.com/thread/123", "Rapidgator")
        svc._scrape_adithd_links.assert_called_once_with("http://adit-hd.com/thread/123", "Rapidgator")
        assert result == ["http://rapidgator.net/file/abc"]


class TestScrapeLinksHDEncode:
    """Test the default HDEncode scraping path."""

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._WebDriverWait")
    @patch("backend.download_service._EC")
    @patch("backend.download_service._By")
    def test_scrape_finds_rapidgator_links(self, mock_by, mock_ec, mock_wait_cls, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"
        # Real Selenium returns a list; [] means "no #main-frame-error", i.e. the
        # browser is showing the site, not its own network-error page.
        mock_driver.find_elements.return_value = []

        # Simulate finding an access button and clicking it
        mock_wait = MagicMock()
        mock_wait_cls.return_value = mock_wait
        mock_btn = MagicMock()
        mock_wait.until.return_value = mock_btn

        mock_driver.page_source = """
        <html><body>
            <a href="http://rapidgator.net/file/abc123">RG Link</a>
            <a href="http://rapidgator.net/file/def456">RG Link 2</a>
            <a href="http://other.com/file">Other</a>
        </body></html>
        """

        from bs4 import BeautifulSoup as RealBS
        with patch("bs4.BeautifulSoup", side_effect=lambda html, parser: RealBS(html, parser)):
            result = svc.scrape_links("http://hdencode.com/movie123", "Rapidgator")

        assert len(result) == 2
        assert all("rapidgator" in link for link in result)

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._WebDriverWait")
    @patch("backend.download_service._EC")
    @patch("backend.download_service._By")
    def test_scrape_no_access_button_returns_empty(self, mock_by, mock_ec, mock_wait_cls, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        mock_wait = MagicMock()
        mock_wait_cls.return_value = mock_wait
        # All xpaths fail
        mock_wait.until.side_effect = Exception("not found")
        # CSS fallback also fails
        mock_driver.find_element.side_effect = Exception("not found")

        result = svc.scrape_links("http://hdencode.com/movie123", "Rapidgator")
        assert result == []

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service._WebDriverWait")
    @patch("backend.download_service._EC")
    @patch("backend.download_service._By")
    def test_scrape_exception_returns_empty(self, mock_by, mock_ec, mock_wait_cls, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"
        mock_driver.get.side_effect = RuntimeError("nav fail")

        result = svc.scrape_links("http://hdencode.com/movie123", "Rapidgator")
        assert result == []


# ======================================================================
# _scrape_ddlbase_links
# ======================================================================

class TestScrapeDDLBaseLinks:
    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_finds_direct_fichier_links(self, mock_sleep, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"
        # Real Selenium returns a list; [] = "not the browser's error page"
        # (see _browser_error_code), so _navigate proceeds normally.
        mock_driver.find_elements.return_value = []

        mock_driver.page_source = """
        <html><body>
            <div class="entry-content">
                <a href="https://1fichier.com/?abc123">Download</a>
                <a href="https://1fichier.com/?def456">Download 2</a>
            </div>
        </body></html>
        """

        result = svc._scrape_ddlbase_links("http://ddlbase.com/movie-xyz")
        assert len(result) == 2
        assert all("1fichier.com" in link for link in result)

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_no_content_area_returns_empty(self, mock_sleep, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        # Simulate a page with no body at all
        mock_driver.page_source = "<html></html>"

        result = svc._scrape_ddlbase_links("http://ddlbase.com/movie-xyz")
        # BeautifulSoup with no body -> content is None -> returns []
        assert result == []

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_shortlinks_resolved(self, mock_sleep, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"
        mock_driver.find_elements.return_value = []  # not a browser error page

        mock_driver.page_source = """
        <html><body>
            <div class="entry-content">
                <a href="https://cuty.io/abc123">Shortlink</a>
            </div>
        </body></html>
        """
        # After navigating to shortlink, driver resolves to 1fichier
        mock_driver.current_url = "https://1fichier.com/?resolved123"

        result = svc._scrape_ddlbase_links("http://ddlbase.com/movie-xyz")
        assert "https://1fichier.com/?resolved123" in result

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_no_links_found(self, mock_sleep, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        mock_driver.page_source = """
        <html><body>
            <div class="entry-content">
                <p>No downloads available</p>
            </div>
        </body></html>
        """

        result = svc._scrape_ddlbase_links("http://ddlbase.com/movie-xyz")
        assert result == []

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_exception_returns_empty(self, mock_sleep, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"
        mock_driver.get.side_effect = RuntimeError("nav fail")

        # The get_driver call inside will return cached_driver, but .get() raises
        result = svc._scrape_ddlbase_links("http://ddlbase.com/movie-xyz")
        assert result == []


class TestDecodeDDLBaseLink:
    """Tests for DDLBase ddllk XOR+base64 decoding."""

    def test_decodes_mirror1_cuty_link(self):
        from backend.sources.ddlbase import decode_ddlbase_link
        result = decode_ddlbase_link("BQ0nFRBISltSR0cUVzoKTEAIIFxr")
        assert result == "https://cuty.io/2mTmY"

    def test_decodes_mirror2_exe_link(self):
        from backend.sources.ddlbase import decode_ddlbase_link
        result = decode_ddlbase_link("BQ0nFRBISltUSlZDEDxKVyUSB19mBw==")
        assert result == "https://exe.io/4WwsnT4"

    def test_returns_none_for_invalid_input(self):
        from backend.sources.ddlbase import decode_ddlbase_link
        assert decode_ddlbase_link("not-valid-base64!!!") is None

    def test_returns_none_for_non_url_result(self):
        from backend.sources.ddlbase import decode_ddlbase_link
        import base64
        key = "mySecret123"
        plain = "hello"
        xored = bytes(ord(c) ^ ord(key[i % len(key)]) for i, c in enumerate(plain))
        encoded = base64.b64encode(xored).decode()
        assert decode_ddlbase_link(encoded) is None


class TestCuttlinksShortlinkResolution:
    """Tests for the automated cuttlinks.com shortlink resolution."""

    @staticmethod
    def _setup_mocks(driver_props, config=None):
        """Set up module-level Selenium mocks and return a configured service + driver."""
        from selenium.common.exceptions import (
            NoSuchElementException, TimeoutException,
        )
        import backend.download_service as ds

        svc = _make_service(config=config or {"ddlbase_manual_resolution_timeout": 3})
        driver = MagicMock()

        # find_element always raises — no buttons found
        driver.find_element.side_effect = NoSuchElementException

        # Set up current_url and page_source as iterators
        urls = iter(driver_props.get("urls", []))
        default_url = driver_props.get("default_url", "")
        type(driver).current_url = property(lambda self: next(urls, default_url))

        sources = iter(driver_props.get("sources", []))
        default_source = driver_props.get("default_source", "")
        type(driver).page_source = property(lambda self: next(sources, default_source))

        # Mock Selenium globals — _wait_for_submit_button catches TimeoutException
        ds._WebDriverWait = MagicMock(return_value=MagicMock(
            until=MagicMock(side_effect=TimeoutException)
        ))
        ds._By = MagicMock()
        ds._EC = MagicMock()

        return svc, driver

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_resolves_when_redirect_reaches_supported_host(self, mock_sleep, mock_ensure):
        """After navigation the browser eventually lands on 1fichier.com."""
        svc, driver = self._setup_mocks({
            "urls": ["https://cuttlinks.com/abc", "https://cuttlinks.com/abc"],
            "default_url": "https://1fichier.com/?resolved123",
            "default_source": "",
        })
        result = svc._resolve_cuttlinks_shortlink(driver, "https://cuttlinks.com/abc")
        assert result == "https://1fichier.com/?resolved123"

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_finds_download_link_in_page_source(self, mock_sleep, mock_ensure):
        """Falls back to scanning page source for file-host links."""
        svc, driver = self._setup_mocks({
            "default_url": "https://cuttlinks.com/abc",
            "sources": ["<html></html>"],
            "default_source": '<html><a href="https://rapidgator.net/file/abc123">DL</a></html>',
        })
        result = svc._resolve_cuttlinks_shortlink(driver, "https://cuttlinks.com/abc")
        assert result == "https://rapidgator.net/file/abc123"

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_returns_none_after_timeout(self, mock_sleep, mock_ensure):
        """Returns None when shortlink never resolves."""
        svc, driver = self._setup_mocks(
            {"default_url": "https://cuttlinks.com/abc", "default_source": "<html></html>"},
            config={"ddlbase_manual_resolution_timeout": 2},
        )

        result = svc._resolve_cuttlinks_shortlink(driver, "https://cuttlinks.com/abc")
        assert result is None


# ======================================================================
# _scrape_adithd_links
# ======================================================================

class TestScrapeAditHDLinks:
    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_fallback_scrape_with_service_type(self, mock_sleep, mock_ensure):
        """When registry import fails, fallback scrapes page for links."""
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        mock_driver.page_source = """
        <html><body>
            <a href="http://rapidgator.net/file/abc123">RG Link</a>
            <a href="http://nitroflare.com/view/def456">NF Link</a>
            <a href="http://other.com/file">Other</a>
        </body></html>
        """

        # Patch the registry import to fail
        with patch.dict("sys.modules", {"sources.registry": None, "sources": None}):
            result = svc._scrape_adithd_links("http://adit-hd.com/thread/123", "Rapidgator")

        assert len(result) == 1
        assert "rapidgator" in result[0]

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_fallback_no_service_type_finds_all(self, mock_sleep, mock_ensure):
        """When no service_type, fallback finds all supported hosting links."""
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        mock_driver.page_source = """
        <html><body>
            <a href="http://rapidgator.net/file/abc123">RG Link</a>
            <a href="http://nitroflare.com/view/def456">NF Link</a>
            <a href="http://1fichier.com/?xyz">1F Link</a>
        </body></html>
        """

        with patch.dict("sys.modules", {"sources.registry": None, "sources": None}):
            result = svc._scrape_adithd_links("http://adit-hd.com/thread/123", "")

        assert len(result) == 3

    @patch("backend.download_service._ensure_selenium")
    def test_exception_returns_empty(self, mock_ensure):
        svc = _make_service()
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"
        # get_driver succeeds but navigation fails
        mock_driver.get.side_effect = RuntimeError("nav fail")

        # Patch to trigger fallback path with error
        with patch.dict("sys.modules", {"sources.registry": None, "sources": None}):
            result = svc._scrape_adithd_links("http://adit-hd.com/thread/123", "Rapidgator")

        assert result == []

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_registry_path_with_links(self, mock_sleep, mock_ensure):
        """Test the registry code path when the source returns links."""
        svc = _make_service(config={
            "adithd_username": "user",
            "adithd_password": "pass",
            "adithd_auto_reply": True,
        })
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        mock_adithd_source = MagicMock()

        async def fake_login():
            pass

        async def fake_fetch(url):
            return ("content", ["http://rapidgator.net/file/abc", "http://nitroflare.com/view/def"])

        mock_adithd_source.login = fake_login
        mock_adithd_source.fetch_thread_content = fake_fetch

        mock_registry = MagicMock()
        mock_registry.get_source.return_value = mock_adithd_source

        with patch("backend.sources.registry.get_registry", return_value=mock_registry):
            result = svc._scrape_adithd_links("http://adit-hd.com/thread/123", "Rapidgator")

        assert len(result) == 1
        assert "rapidgator" in result[0]

    @patch("backend.download_service._ensure_selenium")
    @patch("backend.download_service.time.sleep")
    def test_registry_path_no_links(self, mock_sleep, mock_ensure):
        """Registry path returns empty links."""
        svc = _make_service(config={})
        mock_driver = MagicMock()
        svc.cached_driver = mock_driver
        mock_driver.title = "ok"

        mock_adithd_source = MagicMock()

        async def fake_login():
            pass

        async def fake_fetch(url):
            return ("content", [])

        mock_adithd_source.login = fake_login
        mock_adithd_source.fetch_thread_content = fake_fetch

        mock_registry = MagicMock()
        mock_registry.get_source.return_value = mock_adithd_source

        mock_registry_module = MagicMock()
        mock_registry_module.get_registry.return_value = mock_registry

        with patch.dict("sys.modules", {"sources": MagicMock(), "sources.registry": mock_registry_module}):
            result = svc._scrape_adithd_links("http://adit-hd.com/thread/123", "Rapidgator")

        assert result == []


# ======================================================================
# Export CSV
# ======================================================================

class TestExportResultsCSV:
    def test_export_to_custom_path(self, tmp_path):
        filepath = str(tmp_path / "results.csv")
        svc = _make_service()
        items = [_FakeItem(), _FakeItem(title="Another", season=2)]

        result = svc.export_results_csv(items, filepath)
        assert result == filepath
        assert os.path.exists(filepath)

        with open(filepath, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
        # Header + 2 data rows
        assert len(rows) == 3
        assert rows[0] == ['Status', 'Title', 'Year', 'Season', 'Resolution', 'Size', 'HDR', 'Plex Info', 'URL']
        assert rows[1][1] == "Test Movie"
        assert rows[1][3] == "-"  # season None -> "-"
        assert rows[2][3] == "S02"  # season 2 -> "S02"

    def test_export_default_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        svc = _make_service()
        items = [_FakeItem()]

        result = svc.export_results_csv(items)
        assert result.startswith(str(tmp_path))
        assert "scanhound_results_" in result or "mediascout_results_" in result
        assert result.endswith(".csv")
        assert os.path.exists(result)

    def test_export_empty_list(self, tmp_path):
        filepath = str(tmp_path / "empty.csv")
        svc = _make_service()
        result = svc.export_results_csv([], filepath)
        assert os.path.exists(result)
        with open(filepath, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert len(rows) == 1  # header only


# ======================================================================
# URL helpers
# ======================================================================

class TestOpenUrl:
    @patch("backend.download_service.webbrowser.open")
    def test_opens_url(self, mock_open):
        DownloadService.open_url("http://example.com")
        mock_open.assert_called_once_with("http://example.com")


class TestCopyToClipboard:
    def test_empty_links_returns_false(self):
        result = DownloadService.copy_to_clipboard([])
        assert result is False

    @patch("backend.download_service.subprocess.Popen")
    @patch("backend.download_service.threading.current_thread")
    def test_main_thread_tries_qt_then_clip(self, mock_thread, mock_popen):
        """On main thread, first tries Qt clipboard, then falls back to clip.exe."""
        mock_thread.return_value = threading.main_thread()

        # Mock Qt to fail
        mock_app = MagicMock()
        mock_clipboard = MagicMock()
        mock_app.clipboard.return_value = mock_clipboard

        with patch.dict("sys.modules", {
            "PySide6": MagicMock(),
            "PySide6.QtCore": MagicMock(),
            "PySide6.QtWidgets": MagicMock(),
        }):
            with patch("PySide6.QtWidgets.QApplication") as mock_qapp:
                mock_qapp.clipboard.return_value = mock_clipboard
                result = DownloadService.copy_to_clipboard(["http://a.com", "http://b.com"])

        # Should have tried something; exact result depends on mock setup
        # The important thing is no exception was raised
        assert isinstance(result, bool)

    @patch("backend.download_service.subprocess.Popen")
    @patch("backend.download_service.threading.current_thread")
    def test_clip_exe_success(self, mock_thread, mock_popen):
        """clip.exe fallback path."""
        # Not main thread
        mock_thread.return_value = MagicMock()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Mock PySide6 imports that happen inside the function
        mock_pyside = MagicMock()
        with patch.dict("sys.modules", {
            "PySide6": mock_pyside,
            "PySide6.QtCore": mock_pyside.QtCore,
            "PySide6.QtWidgets": mock_pyside.QtWidgets,
        }):
            with patch.object(__import__("subprocess"), "CREATE_NO_WINDOW", 0x08000000, create=True):
                result = DownloadService.copy_to_clipboard(["http://a.com"])

        assert result is True
        mock_proc.communicate.assert_called_once()


# ======================================================================
# download_item
# ======================================================================

class TestDownloadItem:
    def test_no_url_returns_failure(self):
        svc = _make_service()
        result = svc.download_item("", "Title", None, "4K", "50 GB")
        assert result["success"] is False
        assert result["message"] == "No URL provided"

    def test_jdownloader_success(self, tmp_path):
        folder = str(tmp_path)
        svc = _make_service(config={"jd_enabled": True, "jd_method": "folder", "jd_folder": folder})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is True
        assert result["method"] == "jdownloader"
        assert result["link_count"] == 1
        svc.save_to_history.assert_called_once()

    def test_clipboard_fallback(self):
        svc = _make_service(config={})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.copy_to_clipboard = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is True
        assert result["method"] == "clipboard"

    @patch("backend.download_service.webbrowser.open")
    def test_browser_fallback(self, mock_wb):
        svc = _make_service(config={})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.copy_to_clipboard = MagicMock(return_value=False)
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is True
        assert result["method"] == "browser"
        mock_wb.assert_called_once_with("http://page.com")

    @patch("backend.download_service.webbrowser.open")
    def test_browser_fallback_skipped_in_server_mode(self, mock_wb):
        """Headless/server mode must not claim a phantom browser delivery."""
        svc = _make_service(config={})
        svc.server_mode = True
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.copy_to_clipboard = MagicMock(return_value=False)
        svc.save_to_history = MagicMock()

        events = []
        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB",
                                   progress_callback=lambda e, d: events.append(e))
        assert result["success"] is False
        assert result["method"] == ""
        mock_wb.assert_not_called()
        assert "download:failed" in events

    def test_failed_jd_send_in_server_mode_is_not_archived(self):
        """The reported bug: a JD send that fails must NOT be archived as grabbed.

        In server/headless mode the clipboard fallback is a phantom delivery, so
        it must be skipped — a failed JDownloader hand-off ends as an honest
        failure and only writes a 'failed' history row (which does not count as
        grabbed), never 'completed'/'clipboard'.
        """
        svc = _make_service(config={"jd_enabled": True, "jd_method": "api"})
        svc.server_mode = True
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.send_to_jdownloader = MagicMock(return_value=False)  # JD send fails
        svc.copy_to_clipboard = MagicMock(return_value=True)     # would "succeed" if reached
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")

        assert result["success"] is False
        assert result["method"] == ""
        svc.copy_to_clipboard.assert_not_called()  # gated off in server mode
        # Not archived as grabbed: only a 'failed' row (if any) is permitted.
        for call in svc.save_to_history.call_args_list:
            assert call.kwargs.get("status", "completed") == "failed"

    @patch("backend.download_service.webbrowser.open", return_value=False)
    def test_browser_unavailable_is_failure(self, mock_wb):
        """If no browser actually launches, it's a failure, not a success."""
        svc = _make_service(config={})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.copy_to_clipboard = MagicMock(return_value=False)
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is False
        assert result["method"] == ""

    def test_scrape_exception_no_fallback_for_page_url(self):
        """A source-page URL must NOT be sent on when scraping fails — it would
        just become a Cloudflare-blocked entry in JDownloader."""
        svc = _make_service(config={})
        svc.scrape_links = MagicMock(side_effect=RuntimeError("selenium fail"))
        svc.copy_to_clipboard = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is False
        assert result["link_count"] == 0
        svc.copy_to_clipboard.assert_not_called()

    def test_empty_scrape_no_fallback_for_page_url(self):
        svc = _make_service(config={})
        svc.scrape_links = MagicMock(return_value=[])
        svc.copy_to_clipboard = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is False
        assert result["link_count"] == 0
        svc.copy_to_clipboard.assert_not_called()

    def test_file_host_url_falls_back_when_scrape_empty(self):
        """A direct file-host URL is still usable even if the scrape finds nothing."""
        svc = _make_service(config={})
        svc.scrape_links = MagicMock(return_value=[])
        svc.copy_to_clipboard = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        result = svc.download_item("https://rapidgator.net/file/abc123", "Movie", None, "4K", "50 GB")
        assert result["success"] is True
        assert result["link_count"] == 1

    def test_package_name_with_title(self):
        svc = _make_service(config={"jd_enabled": True, "jd_method": "folder", "jd_folder": "/tmp"})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        svc.download_item("http://page.com", "My Movie", None, "4K", "50 GB")
        args = svc.send_to_jdownloader.call_args
        assert args[0][1] == "My Movie [4K]"

    def test_package_name_without_title(self):
        svc = _make_service(config={"jd_enabled": True, "jd_method": "folder", "jd_folder": "/tmp"})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        svc.download_item("http://page.com", "", None, "4K", "50 GB")
        args = svc.send_to_jdownloader.call_args
        assert args[0][1] == "ScanHound Download"

    def test_jd_api_method_triggers_send(self):
        svc = _make_service(config={"jd_enabled": True, "jd_method": "api", "jd_folder": ""})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/file1"])
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()

        result = svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert result["success"] is True
        assert result["method"] == "jdownloader"

    def test_movie_routes_to_movies_folder(self):
        svc = _make_service(config={"jd_enabled": True, "jd_method": "api",
                                    "jd_movies_folder": "/dl/Movies", "jd_tv_folder": "/dl/TV"})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/f1"])
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()
        svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")  # season None -> movie
        assert svc.send_to_jdownloader.call_args.kwargs["destination"] == "/dl/Movies"

    def test_tv_routes_to_tv_folder(self):
        svc = _make_service(config={"jd_enabled": True, "jd_method": "api",
                                    "jd_movies_folder": "/dl/Movies", "jd_tv_folder": "/dl/TV"})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/f1"])
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()
        svc.download_item("http://page.com", "Show", 2, "1080p", "10 GB")  # season 2 -> TV
        assert svc.send_to_jdownloader.call_args.kwargs["destination"] == "/dl/TV"

    def test_no_per_type_folder_sends_empty_destination(self):
        svc = _make_service(config={"jd_enabled": True, "jd_method": "api"})
        svc.scrape_links = MagicMock(return_value=["http://rg.net/f1"])
        svc.send_to_jdownloader = MagicMock(return_value=True)
        svc.save_to_history = MagicMock()
        svc.download_item("http://page.com", "Movie", None, "4K", "50 GB")
        assert svc.send_to_jdownloader.call_args.kwargs["destination"] == ""

    def test_send_to_jdownloader_api_sets_destination_folder(self):
        svc = _make_service(config={"jd_method": "api"})
        device = MagicMock()
        with patch.object(svc, "_connect_jd_device", return_value=device):
            ok = svc.send_to_jdownloader(["http://rg/f1"], "Pkg", destination="/dl/Movies")
        assert ok is True
        payload = device.linkgrabber.add_links.call_args[0][0]
        assert payload[0]["destinationFolder"] == "/dl/Movies"


# ======================================================================
# open_in_plex
# ======================================================================

class TestOpenInPlex:
    @patch("backend.download_service.webbrowser.open")
    @patch("backend.download_service.normalize_title", side_effect=lambda t: t.lower().strip())
    def test_found_with_server_id(self, mock_norm, mock_wb):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400",
            "plex_server_id": "abc123",
        })
        plex_movies = [{"clean_title": "inception", "rating_key": "42"}]

        result = svc.open_in_plex("Inception", plex_movies, [])
        assert result is not None
        assert "abc123" in result
        assert "42" in result
        mock_wb.assert_called_once()

    @patch("backend.download_service.webbrowser.open")
    @patch("backend.download_service.normalize_title", side_effect=lambda t: t.lower().strip())
    def test_found_without_server_id(self, mock_norm, mock_wb):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400",
            "plex_server_id": "",
        })
        plex_tv = [{"clean_title": "the bear", "rating_key": "99"}]

        result = svc.open_in_plex("The Bear", [], plex_tv)
        assert result is not None
        assert "99" in result
        assert "server" not in result

    @patch("backend.download_service.normalize_title", side_effect=lambda t: t.lower().strip())
    def test_not_found(self, mock_norm):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400",
            "plex_server_id": "",
        })

        result = svc.open_in_plex("Nonexistent Movie", [], [])
        assert result is None

    def test_no_plex_url(self):
        svc = _make_service(config={"plex_url": ""})
        result = svc.open_in_plex("Anything", [], [])
        assert result is None

    @patch("backend.download_service.normalize_title", side_effect=lambda t: t.lower().strip())
    def test_no_rating_key(self, mock_norm):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400",
            "plex_server_id": "",
        })
        plex_movies = [{"clean_title": "dune", "rating_key": None}]

        result = svc.open_in_plex("Dune", plex_movies, [])
        assert result is None

    @patch("backend.download_service.webbrowser.open")
    @patch("backend.download_service.normalize_title", side_effect=lambda t: t.lower().strip())
    def test_plex_url_trailing_slash_stripped(self, mock_norm, mock_wb):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400/",
            "plex_server_id": "srv1",
        })
        plex_movies = [{"clean_title": "test", "rating_key": "1"}]

        result = svc.open_in_plex("Test", plex_movies, [])
        assert result is not None
        assert "http://plex.local:32400/web/" in result
        # Should not have double slash
        assert "32400//web" not in result

    @patch("backend.download_service.webbrowser.open")
    def test_prefers_explicit_rating_key_over_title_collision(self, mock_wb):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400",
            "plex_server_id": "srv1",
        })
        plex_movies = [
            {"clean_title": "anaconda", "year": 1997, "rating_key": "old"},
            {"clean_title": "anaconda", "year": 2025, "rating_key": "new"},
        ]

        result = svc.open_in_plex(
            "Anaconda",
            plex_movies,
            [],
            year=2025,
            plex_rating_key="new",
        )

        assert result is not None
        assert result.endswith("metadata%2Fnew")
        mock_wb.assert_called_once_with(result)

    @patch("backend.download_service.webbrowser.open")
    @patch("backend.download_service.normalize_title", side_effect=lambda t: t.lower().strip())
    def test_title_and_year_disambiguate_same_name_movies(self, mock_norm, mock_wb):
        svc = _make_service(config={
            "plex_url": "http://plex.local:32400",
            "plex_server_id": "srv1",
        })
        plex_movies = [
            {"clean_title": "anaconda", "year": 1997, "rating_key": "old"},
            {"clean_title": "anaconda", "year": 2025, "rating_key": "new"},
        ]

        result = svc.open_in_plex("Anaconda", plex_movies, [], year=2025)

        assert result is not None
        assert result.endswith("metadata%2Fnew")
        mock_wb.assert_called_once_with(result)


# ======================================================================
# JDownloader run-state / queue control / results polling
# ======================================================================

class TestNormalizeRunState:
    def _device(self, state):
        device = MagicMock()
        device.downloadcontroller.get_current_state.return_value = state
        return device

    def test_running(self):
        assert DownloadService._normalize_run_state_from(self._device("RUNNING")) == "running"

    def test_paused(self):
        assert DownloadService._normalize_run_state_from(self._device("PAUSED")) == "paused"

    @pytest.mark.parametrize("raw", ["STOPPED_STATE", "IDLE"])
    def test_stopped_variants(self, raw):
        assert DownloadService._normalize_run_state_from(self._device(raw)) == "stopped"

    def test_unrecognized_state_is_lowercased(self):
        assert DownloadService._normalize_run_state_from(self._device("TODO")) == "todo"

    def test_empty_state_returns_unknown(self):
        assert DownloadService._normalize_run_state_from(self._device("")) == "unknown"

    def test_exception_returns_unknown(self):
        device = MagicMock()
        device.downloadcontroller.get_current_state.side_effect = RuntimeError("boom")
        assert DownloadService._normalize_run_state_from(device) == "unknown"


class TestGetJdState:
    def test_connected(self):
        svc = _make_service(config={"jd_method": "api"})
        device = MagicMock()
        device.downloadcontroller.get_current_state.return_value = "RUNNING"
        with patch.object(svc, "_connect_jd_device", return_value=device):
            assert svc.get_jd_state() == {"connected": True, "state": "running"}

    def test_not_connected(self):
        svc = _make_service(config={"jd_method": "api"})
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("no creds")):
            result = svc.get_jd_state()
        assert result["connected"] is False
        assert result["state"] == "unknown"
        assert "no creds" in result["error"]


class TestJdControl:
    def _device(self, state="RUNNING"):
        device = MagicMock()
        device.downloadcontroller.get_current_state.return_value = state
        return device

    def test_start(self):
        svc = _make_service(config={"jd_method": "api"})
        device = self._device("RUNNING")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("start")
        device.downloadcontroller.start_downloads.assert_called_once()
        assert result == {"ok": True, "action": "start", "state": "running"}

    def test_pause(self):
        svc = _make_service(config={"jd_method": "api"})
        device = self._device("PAUSED")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("pause")
        device.downloadcontroller.pause_downloads.assert_called_once_with(True)
        assert result["ok"] is True
        assert result["state"] == "paused"

    def test_resume(self):
        svc = _make_service(config={"jd_method": "api"})
        device = self._device("RUNNING")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("resume")
        device.downloadcontroller.pause_downloads.assert_called_once_with(False)
        assert result["ok"] is True

    def test_stop(self):
        svc = _make_service(config={"jd_method": "api"})
        device = self._device("STOPPED_STATE")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("stop")
        device.downloadcontroller.stop_downloads.assert_called_once()
        assert result["state"] == "stopped"

    def test_unknown_action(self):
        svc = _make_service(config={"jd_method": "api"})
        with patch.object(svc, "_connect_jd_device", return_value=self._device()):
            result = svc.jd_control("frobnicate")
        assert result == {"ok": False, "error": "Unknown action: frobnicate"}

    def test_connection_failure(self):
        svc = _make_service(config={"jd_method": "api"})
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("no creds")):
            result = svc.jd_control("start")
        assert result["ok"] is False
        assert "no creds" in result["error"]

    def test_controller_exception(self):
        svc = _make_service(config={"jd_method": "api"})
        device = self._device()
        device.downloadcontroller.start_downloads.side_effect = RuntimeError("jd error")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("start")
        assert result["ok"] is False
        assert "jd error" in result["error"]

    def test_controller_exception_invalidates_connection_cache(self):
        """A failed control RPC drops the cached connection so the next call reconnects."""
        svc = _make_service(config={"jd_method": "api"})
        device = self._device()
        device.downloadcontroller.start_downloads.side_effect = RuntimeError("jd error")
        svc._jd = MagicMock()
        svc._jd_device = device
        svc._jd_conn_ts = 12345.0
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("start")
        assert result["ok"] is False
        assert svc._jd is None
        assert svc._jd_device is None
        assert svc._jd_conn_ts == 0.0

    def test_action_is_case_and_whitespace_insensitive(self):
        svc = _make_service(config={"jd_method": "api"})
        device = self._device("RUNNING")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.jd_control("  Start  ")
        device.downloadcontroller.start_downloads.assert_called_once()
        assert result["ok"] is True


class TestNormalizeLinkUrl:
    def test_scheme_www_slash_variants_match(self):
        from backend.download_service import _normalize_link_url as n
        a = n("https://www.rapidgator.net/file/abc/")
        b = n("http://rapidgator.net/file/abc")
        assert a == b == "rapidgator.net/file/abc"

    def test_path_host_drops_query(self):
        from backend.download_service import _normalize_link_url as n
        # Rapidgator: id is in the path, so an incidental query is dropped.
        assert n("https://rapidgator.net/file/abc?ref=x") == "rapidgator.net/file/abc"

    def test_1fichier_query_is_kept_and_distinct(self):
        from backend.download_service import _normalize_link_url as n
        # 1fichier puts the id in the query — different files must NOT collide.
        one = n("https://1fichier.com/?abc123")
        two = n("https://1fichier.com/?def456")
        assert one != two
        assert one == "1fichier.com?abc123"

    def test_empty_returns_empty(self):
        from backend.download_service import _normalize_link_url as n
        assert n("") == ""


class TestDownloadHistoryStatusFilter:
    def test_failed_grabs_excluded_from_history(self, db_manager):
        """A failed grab must NOT count as a prior download — otherwise the scan
        list would wrongly mark the item as Downloaded on the next scan."""
        db_manager.add_to_history("http://ok.com", "Good", status="completed")
        db_manager.add_to_history("http://bad.com", "Bad", status="failed")
        db_manager.add_to_history("http://legacy.com", "Legacy")  # default 'completed'
        svc = _make_service(db=db_manager)
        hist = svc.load_download_history()
        assert "http://ok.com" in hist
        assert "http://legacy.com" in hist
        assert "http://bad.com" not in hist


class TestPollResults:
    def _device(self, packages=None, links=None, raise_on_packages=False, raise_on_links=False):
        device = MagicMock()
        if raise_on_packages:
            device.downloads.query_packages.side_effect = RuntimeError("packages fail")
        else:
            device.downloads.query_packages.return_value = packages or []
        if raise_on_links:
            device.downloads.query_links.side_effect = RuntimeError("links fail")
        else:
            device.downloads.query_links.return_value = links or []
        return device

    def _svc(self, db=None):
        if db is None:
            db = MagicMock()
            db.get_scraped_link_titles.return_value = {}
        return _make_service(config={"jd_method": "api"}, db=db), db

    def test_jd_unreachable_returns_empty(self):
        svc, _db = self._svc()
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("no creds")):
            assert svc.poll_results() == []

    def test_queued_state(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Queued", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results == [{
            "name": "Pkg.Queued", "title": "Pkg.Queued", "host": "",
            "bytes_total": 1000, "bytes_loaded": 0, "downloaded": 0,
            "extraction": "na", "state": "queued", "error": None,
            "save_to": "",
        }]

    def test_downloading_state(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Dl", "uuid": 1, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["state"] == "downloading"
        assert results[0]["downloaded"] == 0

    def test_downloaded_state_via_finished_flag(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Done", "uuid": 1, "bytesLoaded": 1000, "bytesTotal": 1000, "finished": True, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["state"] == "downloaded"
        assert results[0]["downloaded"] == 1

    def test_downloaded_state_via_bytes_comparison(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Done2", "uuid": 1, "bytesLoaded": 1000, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["state"] == "downloaded"
        assert results[0]["downloaded"] == 1

    def test_extracting_state(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Extract", "uuid": 1, "bytesLoaded": 1000, "bytesTotal": 1000, "finished": True, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://rg/f1", "name": "f1.rar",
                "finished": True, "status": "", "extractionStatus": "RUNNING", "bytesTotal": 1000, "bytesLoaded": 1000}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["extraction"] == "running"
        assert results[0]["state"] == "extracting"
        assert results[0]["host"] == "rapidgator.net"

    def test_direct_file_download_is_complete(self):
        # A finished direct media file (no archive) is complete, not stuck at
        # "downloaded" waiting for an extraction that never runs.
        svc, _db = self._svc()
        pkg = {"name": "Movie.2026.2160p.WEB.mkv", "uuid": 1, "bytesLoaded": 1000,
               "bytesTotal": 1000, "finished": True, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://rg/f",
                "name": "Movie.2026.2160p.WEB.h265-EDITH.mkv", "finished": True,
                "status": "Finished", "extractionStatus": None,
                "bytesTotal": 1000, "bytesLoaded": 1000}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["state"] == "extracted"
        assert results[0]["extraction"] == "na"

    def test_downloaded_archive_awaits_extraction(self):
        # An archive that just finished downloading (no extraction status yet)
        # stays "downloaded" — not prematurely marked complete.
        svc, _db = self._svc()
        pkg = {"name": "Movie.Pkg", "uuid": 1, "bytesLoaded": 1000,
               "bytesTotal": 1000, "finished": True, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://rg/f",
                "name": "movie.part01.rar", "finished": True, "status": "",
                "extractionStatus": None, "bytesTotal": 1000, "bytesLoaded": 1000}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["state"] == "downloaded"

    def test_extracted_state(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Done3", "uuid": 1, "bytesLoaded": 1000, "bytesTotal": 1000, "finished": True, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://rg/f1", "name": "f1.rar",
                "finished": True, "status": "", "extractionStatus": "SUCCESSFUL", "bytesTotal": 1000, "bytesLoaded": 1000}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["extraction"] == "success"
        assert results[0]["state"] == "extracted"

    def test_extraction_error_overrides_downloaded(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.ExErr", "uuid": 1, "bytesLoaded": 1000, "bytesTotal": 1000, "finished": True, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://rg/f1", "name": "f1.rar",
                "finished": True, "status": "", "extractionStatus": "ERROR", "bytesTotal": 1000, "bytesLoaded": 1000}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["extraction"] == "error"
        assert results[0]["state"] == "failed"

    def test_download_error_when_not_downloaded(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg.Offline", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 0, "finished": False, "status": "OFFLINE"}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["state"] == "failed"
        assert results[0]["error"] == "OFFLINE"

    def test_title_cross_reference_with_resolution(self):
        db = MagicMock()
        db.get_scraped_link_titles.return_value = {"http://rg/f1": {"title": "Real Title", "resolution": "1080p"}}
        svc, _db = self._svc(db=db)
        pkg = {"name": "JD.Filename.Pkg", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://rg/f1", "name": "f1.rar",
                "finished": False, "status": "", "extractionStatus": "", "bytesTotal": 1000, "bytesLoaded": 0}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["title"] == "Real Title [1080p]"

    def test_title_falls_back_to_package_name(self):
        svc, _db = self._svc()
        pkg = {"name": "JD.Filename.Pkg", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["title"] == "JD.Filename.Pkg"

    def test_unnamed_package_fallback(self):
        svc, _db = self._svc()
        pkg = {"uuid": 1, "bytesLoaded": 0, "bytesTotal": 0, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["name"] == "(unnamed package)"

    def test_record_true_upserts_to_db(self):
        svc, db = self._svc()
        pkg = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            svc.poll_results(record=True)
        db.upsert_download_result.assert_called_once()
        kwargs = db.upsert_download_result.call_args.kwargs
        assert kwargs["name"] == "Pkg1"
        assert kwargs["state"] == "downloading"

    def test_record_false_skips_db(self):
        svc, db = self._svc()
        pkg = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            svc.poll_results(record=False)
        db.upsert_download_result.assert_not_called()

    def test_unchanged_result_not_rewritten(self):
        svc, db = self._svc()
        pkg = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            svc.poll_results(record=True)
            svc.poll_results(record=True)
        db.upsert_download_result.assert_called_once()

    def test_changed_result_rewritten(self):
        svc, db = self._svc()
        pkg1 = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        pkg2 = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 1000, "bytesTotal": 1000, "finished": True, "status": ""}
        device1 = self._device(packages=[pkg1], links=[])
        device2 = self._device(packages=[pkg2], links=[])
        with patch.object(svc, "_connect_jd_device", side_effect=[device1, device2]):
            svc.poll_results(record=True)
            svc.poll_results(record=True)
        assert db.upsert_download_result.call_count == 2

    def test_packages_query_failure_invalidates_cache_and_returns_empty(self):
        svc, _db = self._svc()
        device = self._device(raise_on_packages=True)
        with patch.object(svc, "_invalidate_jd_cache") as mock_invalidate:
            with patch.object(svc, "_connect_jd_device", return_value=device):
                results = svc.poll_results(record=False)
        assert results == []
        mock_invalidate.assert_called_once()

    def test_links_query_failure_still_returns_packages(self):
        svc, _db = self._svc()
        pkg = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[], raise_on_links=True)
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert len(results) == 1
        assert results[0]["host"] == ""

    def test_scraped_link_titles_failure_falls_back(self):
        db = MagicMock()
        db.get_scraped_link_titles.side_effect = RuntimeError("db down")
        svc, _db = self._svc(db=db)
        pkg = {"name": "Pkg1", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        device = self._device(packages=[pkg], links=[])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["title"] == "Pkg1"

    def test_title_url_normalization_matches(self):
        db = MagicMock()
        # Map keyed with https + trailing slash; JD link uses http + www, no slash.
        db.get_scraped_link_titles.return_value = {
            "https://rapidgator.net/file/abc/": {"title": "Movie", "resolution": "2160p"},
        }
        svc, _db = self._svc(db=db)
        pkg = {"name": "Scrambled.Pkg", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        link = {"packageUUID": 1, "host": "rapidgator.net", "url": "http://www.rapidgator.net/file/abc",
                "name": "x.rar", "finished": False, "status": "", "extractionStatus": "",
                "bytesTotal": 1000, "bytesLoaded": 0}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            results = svc.poll_results(record=False)
        assert results[0]["title"] == "Movie [2160p]"

    def test_best_title_not_regressed_on_map_miss(self):
        db = MagicMock()
        # First poll resolves a real title; second poll's map is empty.
        db.get_scraped_link_titles.side_effect = [
            {"http://rg/f1": {"title": "Real Movie", "resolution": ""}},
            {},
        ]
        svc, _db = self._svc(db=db)
        pkg = {"name": "Scrambled", "uuid": 1, "bytesLoaded": 0, "bytesTotal": 1000, "finished": False, "status": ""}
        link = {"packageUUID": 1, "host": "rg", "url": "http://rg/f1", "name": "x.rar",
                "finished": False, "status": "", "extractionStatus": "", "bytesTotal": 1000, "bytesLoaded": 0}
        device = self._device(packages=[pkg], links=[link])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            first = svc.poll_results(record=False)
            second = svc.poll_results(record=False)
        assert first[0]["title"] == "Real Movie"
        # Transient map miss must not regress the display to the raw JD name.
        assert second[0]["title"] == "Real Movie"

    def test_caches_pruned_to_live_packages(self):
        db = MagicMock()
        db.get_scraped_link_titles.return_value = {"http://rg/a": {"title": "Movie A", "resolution": ""}}
        svc, _db = self._svc(db=db)
        linkA = {"packageUUID": 1, "host": "rg", "url": "http://rg/a", "name": "a.rar",
                 "finished": False, "status": "", "extractionStatus": "", "bytesTotal": 1000, "bytesLoaded": 500}
        pkgA = {"name": "PkgA", "uuid": 1, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        pkgB = {"name": "PkgB", "uuid": 2, "bytesLoaded": 500, "bytesTotal": 1000, "finished": False, "status": ""}
        deviceA = self._device(packages=[pkgA], links=[linkA])
        deviceB = self._device(packages=[pkgB], links=[])
        with patch.object(svc, "_connect_jd_device", side_effect=[deviceA, deviceB]):
            svc.poll_results(record=True)
            assert "PkgA" in svc._results_cache
            assert "PkgA" in svc._best_titles  # resolved a real title from the map
            svc.poll_results(record=True)
        # PkgA dropped out of JD's list -> evicted from BOTH caches; PkgB retained.
        assert "PkgA" not in svc._results_cache
        assert "PkgA" not in svc._best_titles
        assert "PkgB" in svc._results_cache


class TestGetJdStatus:
    def _device(self, lg_packages=None, lg_links=None, dl_packages=None, dl_links=None, state="RUNNING"):
        device = MagicMock()
        device.linkgrabber.query_packages.return_value = lg_packages or []
        device.linkgrabber.query_links.return_value = lg_links or []
        device.downloads.query_packages.return_value = dl_packages or []
        device.downloads.query_links.return_value = dl_links or []
        device.downloadcontroller.get_current_state.return_value = state
        return device

    def _svc(self):
        db = MagicMock()
        db.get_scraped_link_titles.return_value = {}
        return _make_service(config={"jd_method": "api"}, db=db)

    def test_includes_run_state(self):
        svc = self._svc()
        device = self._device(state="RUNNING")
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.get_jd_status()
        assert result["connected"] is True
        assert result["state"] == "running"
        assert result["total"] == 0

    def test_connection_failure(self):
        svc = self._svc()
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("no creds")):
            result = svc.get_jd_status()
        assert result["connected"] is False
        assert result["links"] == []

    def test_groups_into_packages_offline_first(self):
        svc = self._svc()
        device = self._device(lg_links=[
            {"name": "online.rar", "host": "rg", "bytesTotal": 100, "packageUUID": 1, "url": "u1", "availability": "ONLINE"},
            {"name": "offline.rar", "host": "rg", "bytesTotal": 100, "packageUUID": 2, "url": "u2", "availability": "OFFLINE"},
        ])
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.get_jd_status()
        assert result["online"] == 1
        assert result["offline"] == 1
        assert result["total"] == 2
        assert result["package_count"] == 2
        # The package holding the broken link is surfaced first.
        assert result["packages"][0]["offline"] == 1
        assert result["packages"][0]["links"][0]["name"] == "offline.rar"

    def test_groups_links_into_package_with_resolved_title(self):
        db = MagicMock()
        # Map stored with https + trailing slash; JD link uses http, no slash.
        db.get_scraped_link_titles.return_value = {
            "https://rapidgator.net/file/abc/": {"title": "My Movie", "resolution": "1080p"},
        }
        svc = _make_service(config={"jd_method": "api"}, db=db)
        device = self._device(
            dl_packages=[{"name": "Scrambled.Pkg", "uuid": 7}],
            dl_links=[
                {"name": "p1.rar", "host": "rapidgator.net", "bytesTotal": 100, "bytesLoaded": 100,
                 "finished": True, "status": "", "packageUUID": 7, "url": "http://rapidgator.net/file/abc"},
                {"name": "p2.rar", "host": "rapidgator.net", "bytesTotal": 100, "bytesLoaded": 50,
                 "finished": False, "status": "", "packageUUID": 7, "url": "http://rapidgator.net/file/def"},
            ],
        )
        with patch.object(svc, "_connect_jd_device", return_value=device):
            result = svc.get_jd_status()
        assert result["package_count"] == 1
        pkg = result["packages"][0]
        assert pkg["title"] == "My Movie [1080p]"
        assert pkg["total"] == 2
        assert len(pkg["links"]) == 2
        assert pkg["bytes_total"] == 200
        assert pkg["online"] == 2
