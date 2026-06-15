"""Tests for backend/link_scraper.py — LinkScraper class.

Covers the uncovered 89% of link_scraper.py:
- _VALID_SHORTLINK_DOMAINS constant
- resolve_cuty_link: URL validation, 1fichier found via page, via redirect, not found, exception
- open_cuty_in_browser: success and failure
- resolve_cuty_links_batch: resolved, failed (triggers browser), empty list
- scrape_links_with_driver: exception, no button found, links collected
"""

import pytest
from unittest.mock import MagicMock, patch, call

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.link_scraper import LinkScraper, _VALID_SHORTLINK_DOMAINS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(debug=False):
    app = MagicMock()
    app.config = {"debug_mode": debug}
    return app


def _make_scraper(debug=False):
    return LinkScraper(_make_app(debug=debug))


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------

class TestValidShortlinkDomains:
    def test_cuty_io_present(self):
        assert "cuty.io" in _VALID_SHORTLINK_DOMAINS

    def test_cuttlinks_present(self):
        assert "cuttlinks.com" in _VALID_SHORTLINK_DOMAINS

    def test_is_list_of_strings(self):
        assert all(isinstance(d, str) for d in _VALID_SHORTLINK_DOMAINS)


# ---------------------------------------------------------------------------
# resolve_cuty_link
# ---------------------------------------------------------------------------

class TestResolveCutyLink:

    def test_none_url_returns_none(self):
        scraper = _make_scraper()
        assert scraper.resolve_cuty_link(MagicMock(), None) is None

    def test_empty_url_returns_none(self):
        scraper = _make_scraper()
        assert scraper.resolve_cuty_link(MagicMock(), "") is None

    def test_non_shortlink_domain_returns_none(self):
        scraper = _make_scraper()
        result = scraper.resolve_cuty_link(MagicMock(), "https://example.com/abc")
        assert result is None

    def test_https_google_returns_none(self):
        scraper = _make_scraper()
        result = scraper.resolve_cuty_link(MagicMock(), "https://google.com/xyz")
        assert result is None

    def test_driver_get_exception_returns_none(self):
        scraper = _make_scraper()
        driver = MagicMock()
        driver.get.side_effect = Exception("WebDriver crashed")
        result = scraper.resolve_cuty_link(driver, "https://cuty.io/abc")
        assert result is None

    def test_1fichier_found_in_page_source(self):
        """resolve_cuty_link extracts 1fichier.com link from page HTML."""
        scraper = _make_scraper()
        driver = MagicMock()
        driver.current_url = "https://cuty.io/abc"
        driver.page_source = '<a href="https://1fichier.com/file/xyz">Download</a>'

        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.side_effect = TimeoutException()
            driver.find_element.side_effect = NoSuchElementException()

            result = scraper.resolve_cuty_link(driver, "https://cuty.io/abc")

        assert result == "https://1fichier.com/file/xyz"

    def test_1fichier_found_via_current_url_redirect(self):
        """If the driver was redirected to 1fichier.com, return that URL."""
        scraper = _make_scraper()
        driver = MagicMock()
        driver.current_url = "https://1fichier.com/final-destination"
        driver.page_source = "<html></html>"

        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.side_effect = TimeoutException()
            driver.find_element.side_effect = NoSuchElementException()

            result = scraper.resolve_cuty_link(driver, "https://cuty.io/abc")

        assert result == "https://1fichier.com/final-destination"

    def test_returns_none_when_no_1fichier_link_found(self):
        scraper = _make_scraper()
        driver = MagicMock()
        driver.current_url = "https://cuty.io/still-here"
        driver.page_source = "<html><p>Nothing useful</p></html>"

        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.side_effect = TimeoutException()
            driver.find_element.side_effect = NoSuchElementException()

            result = scraper.resolve_cuty_link(driver, "https://cuty.io/abc")

        assert result is None

    def test_valid_shortlink_domains_all_accepted(self):
        """Every domain in _VALID_SHORTLINK_DOMAINS allows processing to start."""
        scraper = _make_scraper()
        driver = MagicMock()
        driver.get.side_effect = Exception("stop early")

        for domain in _VALID_SHORTLINK_DOMAINS:
            url = f"https://{domain}/testpath"
            result = scraper.resolve_cuty_link(driver, url)
            # exception inside → None (not filtered out by domain check)
            assert result is None

    def test_login_attempted_with_credentials(self):
        """When credentials provided, _cuty_login is called."""
        scraper = _make_scraper()
        driver = MagicMock()
        driver.get.side_effect = Exception("stop after get")

        credentials = {"email": "user@example.com", "password": "pw"}

        with patch.object(scraper, "_cuty_login") as mock_login:
            scraper.resolve_cuty_link(driver, "https://cuty.io/abc", credentials=credentials)

        mock_login.assert_called_once_with(driver, credentials)

    def test_login_failure_does_not_abort(self):
        """If _cuty_login raises, the link resolution continues (exception swallowed)."""
        scraper = _make_scraper()
        driver = MagicMock()
        driver.current_url = "https://cuty.io/still-here"
        driver.page_source = "<html></html>"

        credentials = {"email": "user@test.com", "password": "pw"}

        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        with patch.object(scraper, "_cuty_login", side_effect=Exception("login failed")), \
             patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.side_effect = TimeoutException()
            driver.find_element.side_effect = NoSuchElementException()

            # Should not raise despite login failure
            result = scraper.resolve_cuty_link(driver, "https://cuty.io/abc", credentials=credentials)

        assert result is None  # page had no 1fichier link


# ---------------------------------------------------------------------------
# open_cuty_in_browser
# ---------------------------------------------------------------------------

class TestOpenCutyInBrowser:

    def test_successful_open_returns_true(self):
        scraper = _make_scraper()
        with patch("webbrowser.open", return_value=True) as mock_open:
            result = scraper.open_cuty_in_browser("https://cuty.io/abc")
        assert result is True
        mock_open.assert_called_once_with("https://cuty.io/abc")

    def test_exception_returns_false(self):
        scraper = _make_scraper()
        with patch("webbrowser.open", side_effect=Exception("no browser")):
            result = scraper.open_cuty_in_browser("https://cuty.io/abc")
        assert result is False

    def test_safe_log_called_on_success(self):
        scraper = _make_scraper()
        with patch("webbrowser.open"):
            scraper.open_cuty_in_browser("https://cuty.io/abc")
        scraper.app.safe_log.assert_called()

    def test_safe_log_called_on_failure(self):
        scraper = _make_scraper()
        with patch("webbrowser.open", side_effect=Exception("fail")):
            scraper.open_cuty_in_browser("https://cuty.io/abc")
        scraper.app.safe_log.assert_called()


# ---------------------------------------------------------------------------
# resolve_cuty_links_batch
# ---------------------------------------------------------------------------

class TestResolveCutyLinksBatch:

    def test_empty_list_returns_empty_dict(self):
        scraper = _make_scraper()
        with patch("backend.link_scraper.time.sleep"):
            result = scraper.resolve_cuty_links_batch(MagicMock(), [])
        assert result == {}

    def test_resolved_links_stored(self):
        scraper = _make_scraper()
        driver = MagicMock()

        with patch.object(scraper, "resolve_cuty_link", return_value="https://1fichier.com/x"), \
             patch("backend.link_scraper.time.sleep"):
            result = scraper.resolve_cuty_links_batch(
                driver, ["https://cuty.io/a", "https://cuty.io/b"]
            )

        assert result["https://cuty.io/a"] == "https://1fichier.com/x"
        assert result["https://cuty.io/b"] == "https://1fichier.com/x"

    def test_failed_links_trigger_browser_fallback(self):
        scraper = _make_scraper()
        driver = MagicMock()
        browser_calls = []

        with patch.object(scraper, "resolve_cuty_link", return_value=None), \
             patch.object(scraper, "open_cuty_in_browser", side_effect=browser_calls.append), \
             patch("backend.link_scraper.time.sleep"):
            result = scraper.resolve_cuty_links_batch(driver, ["https://cuty.io/x"])

        assert result["https://cuty.io/x"] is None
        assert "https://cuty.io/x" in browser_calls

    def test_mixed_resolved_and_failed(self):
        scraper = _make_scraper()
        driver = MagicMock()
        browser_calls = []

        def fake_resolve(drv, url, credentials=None):
            return "https://1fichier.com/ok" if "good" in url else None

        with patch.object(scraper, "resolve_cuty_link", side_effect=fake_resolve), \
             patch.object(scraper, "open_cuty_in_browser", side_effect=browser_calls.append), \
             patch("backend.link_scraper.time.sleep"):
            result = scraper.resolve_cuty_links_batch(
                driver,
                ["https://cuty.io/good1", "https://cuty.io/bad1"],
            )

        assert result["https://cuty.io/good1"] == "https://1fichier.com/ok"
        assert result["https://cuty.io/bad1"] is None
        assert "https://cuty.io/bad1" in browser_calls
        assert "https://cuty.io/good1" not in browser_calls

    def test_credentials_passed_to_resolve(self):
        scraper = _make_scraper()
        driver = MagicMock()
        creds = {"email": "a@b.com", "password": "pw"}
        resolved_calls = []

        def fake_resolve(drv, url, credentials=None):
            resolved_calls.append(credentials)
            return None

        with patch.object(scraper, "resolve_cuty_link", side_effect=fake_resolve), \
             patch.object(scraper, "open_cuty_in_browser"), \
             patch("backend.link_scraper.time.sleep"):
            scraper.resolve_cuty_links_batch(driver, ["https://cuty.io/a"], credentials=creds)

        assert resolved_calls[0] == creds

    def test_delay_between_urls(self):
        scraper = _make_scraper()
        driver = MagicMock()
        sleep_calls = []

        with patch.object(scraper, "resolve_cuty_link", return_value=None), \
             patch.object(scraper, "open_cuty_in_browser"), \
             patch("backend.link_scraper.time.sleep", side_effect=sleep_calls.append):
            scraper.resolve_cuty_links_batch(
                driver, ["https://cuty.io/a", "https://cuty.io/b"]
            )

        # One sleep per URL
        assert len(sleep_calls) == 2


# ---------------------------------------------------------------------------
# scrape_links_with_driver
# ---------------------------------------------------------------------------

class TestScrapeLinksWithDriver:

    def test_driver_get_exception_returns_empty(self):
        scraper = _make_scraper()
        driver = MagicMock()
        driver.get.side_effect = Exception("crash")
        result = scraper.scrape_links_with_driver(driver, "https://example.com", "Rapidgator")
        assert result == []

    def test_no_button_found_returns_empty(self):
        scraper = _make_scraper()
        driver = MagicMock()

        from selenium.common.exceptions import TimeoutException, NoSuchElementException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.side_effect = TimeoutException()
            driver.find_element.side_effect = NoSuchElementException()

            result = scraper.scrape_links_with_driver(
                driver, "https://example.com", "Rapidgator"
            )

        assert result == []

    def test_rapidgator_links_collected(self):
        scraper = _make_scraper()
        driver = MagicMock()
        driver.page_source = """
        <html><body>
        <a href="https://rapidgator.net/file/abc">RG1</a>
        <a href="https://rapidgator.net/file/def">RG2</a>
        <a href="https://nitroflare.com/file/xyz">NF1</a>
        </body></html>
        """

        from selenium.common.exceptions import TimeoutException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            btn_mock = MagicMock()
            mock_wait.until.return_value = btn_mock

            result = scraper.scrape_links_with_driver(
                driver, "https://example.com", "Rapidgator"
            )

        assert len(result) == 2
        assert all("rapidgator" in url for url in result)

    def test_nitroflare_links_collected(self):
        scraper = _make_scraper()
        driver = MagicMock()
        driver.page_source = """
        <html><body>
        <a href="https://rapidgator.net/file/abc">RG1</a>
        <a href="https://nitroflare.com/file/xyz">NF1</a>
        <a href="https://nitroflare.com/file/uvw">NF2</a>
        </body></html>
        """

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.return_value = MagicMock()

            result = scraper.scrape_links_with_driver(
                driver, "https://example.com", "Nitroflare"
            )

        assert len(result) == 2
        assert all("nitroflare" in url for url in result)

    def test_no_matching_links_returns_empty_list(self):
        scraper = _make_scraper()
        driver = MagicMock()
        driver.page_source = """
        <html><body>
        <a href="https://other-host.com/file/abc">Other</a>
        </body></html>
        """

        from selenium.common.exceptions import TimeoutException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            # Button click succeeds; but waiting for keyword links times out
            click_wait = MagicMock()
            click_wait.until.return_value = MagicMock()
            keyword_wait = MagicMock()
            keyword_wait.until.side_effect = TimeoutException()
            mock_wait_cls.side_effect = [click_wait, keyword_wait]

            result = scraper.scrape_links_with_driver(
                driver, "https://example.com", "Rapidgator"
            )

        assert result == []

    def test_button_exception_returns_empty(self):
        """If the button-finding block raises unexpectedly, returns [] via except."""
        scraper = _make_scraper()
        driver = MagicMock()
        # driver.get succeeds; execute_script raises during click attempt
        driver.execute_script.side_effect = Exception("script error")

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.return_value = MagicMock()

            result = scraper.scrape_links_with_driver(
                driver, "https://example.com", "Rapidgator"
            )

        assert result == []


# ---------------------------------------------------------------------------
# _cuty_login
# ---------------------------------------------------------------------------

class TestCutyLogin:

    def test_login_fills_email_password_and_clicks(self):
        scraper = _make_scraper()
        driver = MagicMock()
        credentials = {"email": "test@example.com", "password": "secret"}

        email_field = MagicMock()
        password_field = MagicMock()
        login_btn = MagicMock()

        from selenium.common.exceptions import TimeoutException

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.return_value = email_field
            driver.find_element.side_effect = [password_field, login_btn]

            scraper._cuty_login(driver, credentials)

        email_field.clear.assert_called()
        email_field.send_keys.assert_called_with("test@example.com")
        password_field.clear.assert_called()
        password_field.send_keys.assert_called_with("secret")
        login_btn.click.assert_called()

    def test_login_navigates_to_login_url(self):
        scraper = _make_scraper()
        driver = MagicMock()
        credentials = {"email": "a@b.com", "password": "pw"}

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            mock_wait.until.return_value = MagicMock()
            driver.find_element.return_value = MagicMock()

            scraper._cuty_login(driver, credentials)

        driver.get.assert_called_with("https://cuty.io/login")

    def test_login_exception_propagates(self):
        """_cuty_login raises so caller can catch it."""
        scraper = _make_scraper()
        driver = MagicMock()

        with patch("backend.link_scraper.WebDriverWait") as mock_wait_cls, \
             patch("backend.link_scraper.time.sleep"):
            mock_wait = MagicMock()
            mock_wait_cls.return_value = mock_wait
            from selenium.common.exceptions import TimeoutException
            mock_wait.until.side_effect = TimeoutException("no email field")

            with pytest.raises(TimeoutException):
                scraper._cuty_login(driver, {"email": "a@b.com", "password": "pw"})
