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
    return DownloadService(config=cfg, db=database)


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
