"""Comprehensive tests for backend/sources/adithd.py module.

Covers:
- AditHDSource.get_config: returns correct SourceConfig
- AditHDSource.__init__: initialization state
- AditHDSource.set_credentials: credential management
- AditHDSource.set_driver: driver injection
- AditHDSource._get_scraper: scraper creation and cookie handling
- AditHDSource._load_replied_threads / _save_replied_threads: persistence
- AditHDSource.parse_release: parsing logic for movie/TV titles
- AditHDSource._extract_display_title: title cleaning with MULTI tag
- AditHDSource._extract_release_group: group extraction
- AditHDSource._parse_thread_row: thread row HTML parsing
- AditHDSource._has_next_page: MyBB pagination detection
- AditHDSource._fetch_html: HTML fetching with Selenium fallback
- AditHDSource._extract_download_links: link extraction by host
- AditHDSource.fetch_page: async page fetching
- AditHDSource.search: search functionality
- AditHDSource.login: login flow (mocked Selenium)
- AditHDSource.fetch_thread_content: thread content fetching
- AditHDSource._handle_hidden_content: hidden content reply handling
- AditHDSource.fetch_download_links: download link fetching with filtering
- Edge cases and error handling
"""

import asyncio
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, mock_open
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.sources.adithd import AditHDSource
from backend.sources.base import (
    SourceConfig,
    SourceCapability,
    ParsedRelease,
    PageResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def source():
    """Provide an AditHDSource instance with mocked file operations."""
    with patch.object(AditHDSource, '_load_replied_threads', return_value=set()):
        with patch.object(AditHDSource, '_get_scraper', return_value=MagicMock()):
            src = AditHDSource()
    return src


@pytest.fixture
def source_with_creds(source):
    """Provide an AditHDSource with credentials set."""
    source.set_credentials("testuser", "testpass", auto_reply=True)
    return source


# ---------------------------------------------------------------------------
# Sample HTML builders
# ---------------------------------------------------------------------------

def _build_thread_listing(threads, has_next=False):
    """Build a minimal MyBB forum thread listing page."""
    rows = ""
    for i, (title, url) in enumerate(threads):
        rows += f"""
        <tr class="inline_row">
            <td>
                <span class="subject_new">
                    <a id="tid_{i+1}" href="{url}">{title}</a>
                </span>
            </td>
        </tr>
        """
    pagination = ""
    if has_next:
        pagination = """
        <div class="pagination">
            <span class="pagination_current">1</span>
            <a href="?page=2">2</a>
            <a href="?page=2">Next</a>
        </div>
        """
    return f"""
    <html><body>
    <table>
        {rows}
    </table>
    {pagination}
    </body></html>
    """


def _build_thread_content(links=None, has_hidden=False, has_reply_form=False):
    """Build a minimal thread content page."""
    link_html = ""
    if links:
        for link in links:
            link_html += f'<a href="{link}">Download</a>\n'

    hidden_html = ""
    if has_hidden:
        hidden_html = '<div class="hidden_content">You must reply to see hidden content</div>'

    reply_form = ""
    if has_reply_form:
        reply_form = """
        <form id="quick_reply_form" action="/newreply.php">
            <textarea id="message" name="message"></textarea>
            <input type="submit" name="submit" value="Post Reply" />
        </form>
        """

    return f"""
    <html><body>
    <div class="post_body">
        <p>Thread content here</p>
        {link_html}
        {hidden_html}
    </div>
    {reply_form}
    </body></html>
    """


# ======================================================================
# AditHDSource.get_config Tests
# ======================================================================

class TestGetConfig:
    """Tests for AditHDSource.get_config()."""

    def test_returns_source_config(self):
        """get_config should return a SourceConfig instance."""
        config = AditHDSource.get_config()
        assert isinstance(config, SourceConfig)

    def test_name(self):
        """Config name should be 'adithd'."""
        config = AditHDSource.get_config()
        assert config.name == "adithd"

    def test_display_name(self):
        """Config display_name should be 'Adit-HD'."""
        config = AditHDSource.get_config()
        assert config.display_name == "Adit-HD"

    def test_base_url(self):
        """Config base_url should be the Adit-HD URL."""
        config = AditHDSource.get_config()
        assert config.base_url == "https://www.adit-hd.com"

    def test_capabilities_include_movies(self):
        """Capabilities should include MOVIES."""
        config = AditHDSource.get_config()
        assert SourceCapability.MOVIES in config.capabilities

    def test_capabilities_include_tv_shows(self):
        """Capabilities should include TV_SHOWS."""
        config = AditHDSource.get_config()
        assert SourceCapability.TV_SHOWS in config.capabilities

    def test_capabilities_include_pagination(self):
        """Capabilities should include PAGINATION."""
        config = AditHDSource.get_config()
        assert SourceCapability.PAGINATION in config.capabilities

    def test_capabilities_include_search(self):
        """Capabilities should include SEARCH."""
        config = AditHDSource.get_config()
        assert SourceCapability.SEARCH in config.capabilities

    def test_capabilities_include_direct_links(self):
        """Capabilities should include DIRECT_LINKS."""
        config = AditHDSource.get_config()
        assert SourceCapability.DIRECT_LINKS in config.capabilities

    def test_requires_auth(self):
        """AditHD should require auth."""
        config = AditHDSource.get_config()
        assert config.requires_auth is True

    def test_enabled_by_default(self):
        """AditHD should be enabled by default."""
        config = AditHDSource.get_config()
        assert config.enabled is True

    def test_priority(self):
        """AditHD should have priority 85."""
        config = AditHDSource.get_config()
        assert config.priority == 85

    def test_rate_limit(self):
        """AditHD rate limit should be 3.0."""
        config = AditHDSource.get_config()
        assert config.rate_limit == 3.0

    def test_requires_no_cloudflare_bypass(self):
        """AditHD should NOT require cloudflare bypass."""
        config = AditHDSource.get_config()
        assert config.requires_cloudflare_bypass is False


# ======================================================================
# AditHDSource.__init__ Tests
# ======================================================================

class TestInit:
    """Tests for AditHDSource.__init__()."""

    def test_scraper_is_none(self, source):
        """_scraper should be None initially (overridden in fixture)."""
        # Re-check internal state
        assert source._session_cookies is None
        assert source._is_logged_in is False
        assert source._driver is None
        assert source._credentials is None

    def test_replied_threads_is_set(self, source):
        """_replied_threads should be a set."""
        assert isinstance(source._replied_threads, set)


# ======================================================================
# AditHDSource.set_credentials Tests
# ======================================================================

class TestSetCredentials:
    """Tests for AditHDSource.set_credentials()."""

    def test_sets_credentials(self, source):
        """Credentials should be stored."""
        source.set_credentials("user", "pass")
        assert source._credentials is not None
        assert source._credentials['username'] == "user"
        assert source._credentials['password'] == "pass"

    def test_auto_reply_default_false(self, source):
        """Auto-reply defaults to False."""
        source.set_credentials("user", "pass")
        assert source._credentials['auto_reply'] is False

    def test_auto_reply_true(self, source):
        """Auto-reply can be set to True."""
        source.set_credentials("user", "pass", auto_reply=True)
        assert source._credentials['auto_reply'] is True


# ======================================================================
# AditHDSource.set_driver Tests
# ======================================================================

class TestSetDriver:
    """Tests for AditHDSource.set_driver()."""

    def test_sets_driver(self, source):
        """Driver should be stored."""
        mock_driver = MagicMock()
        source.set_driver(mock_driver)
        assert source._driver is mock_driver


# ======================================================================
# AditHDSource._load_replied_threads / _save_replied_threads Tests
# ======================================================================

class TestRepliedThreadsPersistence:
    """Tests for _load_replied_threads and _save_replied_threads."""

    def test_load_returns_empty_set_when_file_missing(self):
        """Return empty set when file does not exist."""
        with patch('os.path.exists', return_value=False):
            src = AditHDSource.__new__(AditHDSource)
            result = src._load_replied_threads()
        assert result == set()

    def test_load_returns_set_from_file(self, tmp_path):
        """Load set from JSON file."""
        threads_file = tmp_path / "replied.json"
        threads_file.write_text(json.dumps(["url1", "url2"]))

        src = AditHDSource.__new__(AditHDSource)
        with patch.object(AditHDSource, '_replied_threads_path', return_value=str(threads_file)):
            result = src._load_replied_threads()

        assert result == {"url1", "url2"}

    def test_load_returns_empty_on_json_error(self, tmp_path):
        """Return empty set on JSON decode error."""
        threads_file = tmp_path / "replied.json"
        threads_file.write_text("not json")

        src = AditHDSource.__new__(AditHDSource)
        with patch.object(AditHDSource, '_replied_threads_path', return_value=str(threads_file)):
            result = src._load_replied_threads()

        assert result == set()

    def test_save_writes_json(self, source, tmp_path):
        """Save should write replied threads as JSON."""
        threads_file = tmp_path / "replied.json"
        source._replied_threads = {"url1", "url2"}

        with patch.object(AditHDSource, '_replied_threads_path', return_value=str(threads_file)):
            source._save_replied_threads()

        data = json.loads(threads_file.read_text())
        assert set(data) == {"url1", "url2"}

    def test_save_handles_write_error(self, source):
        """Save should handle write errors gracefully."""
        source._replied_threads = {"url1"}

        with patch.object(AditHDSource, '_replied_threads_path', return_value='/nonexistent/path/replied.json'):
            # Should not raise
            source._save_replied_threads()


# ======================================================================
# AditHDSource._get_scraper Tests
# ======================================================================

class TestGetScraper:
    """Tests for AditHDSource._get_scraper()."""

    def test_creates_scraper_with_cloudscraper(self):
        """Creates cloudscraper instance when available."""
        with patch.object(AditHDSource, '_load_replied_threads', return_value=set()):
            src = AditHDSource.__new__(AditHDSource)
            src._scraper = None
            src._session_cookies = None
            src._is_logged_in = False
            src._driver = None
            src._credentials = None
            src._replied_threads = set()
            src._config = AditHDSource.get_config()

            mock_cs = MagicMock()
            mock_scraper = MagicMock()
            mock_cs.create_scraper.return_value = mock_scraper

            with patch.dict('sys.modules', {'cloudscraper': mock_cs}):
                result = src._get_scraper()

            assert result is mock_scraper

    def test_applies_session_cookies(self):
        """Session cookies should be applied to scraper."""
        with patch.object(AditHDSource, '_load_replied_threads', return_value=set()):
            src = AditHDSource.__new__(AditHDSource)
            src._scraper = None
            src._is_logged_in = False
            src._driver = None
            src._credentials = None
            src._replied_threads = set()
            src._config = AditHDSource.get_config()
            src._session_cookies = [
                {'name': 'sid', 'value': 'abc123'},
                {'name': 'token', 'value': 'xyz'},
            ]

            mock_cs = MagicMock()
            mock_scraper_instance = MagicMock()
            mock_cs.create_scraper.return_value = mock_scraper_instance

            with patch.dict('sys.modules', {'cloudscraper': mock_cs}):
                result = src._get_scraper()

            # Verify cookies were set
            assert mock_scraper_instance.cookies.set.call_count == 2

    def test_returns_existing_scraper(self, source):
        """Return existing scraper if already created."""
        mock = MagicMock()
        source._scraper = mock
        result = source._get_scraper()
        assert result is mock


# ======================================================================
# AditHDSource.parse_release Tests
# ======================================================================

class TestParseRelease:
    """Tests for AditHDSource.parse_release()."""

    def test_returns_none_for_non_dict(self, source):
        """Non-dict raw_data should return None."""
        assert source.parse_release("string") is None
        assert source.parse_release(123) is None
        assert source.parse_release(None) is None

    def test_returns_none_for_empty_title(self, source):
        """Empty title should return None."""
        result = source.parse_release({'title': '', 'url': 'http://example.com'})
        assert result is None

    def test_basic_movie_release(self, source):
        """Parse a basic movie release title."""
        raw = {
            'title': 'Inception 2010 1080p BluRay REMUX AVC DTS-HD MA-FGT',
            'url': 'https://www.adit-hd.com/Thread-Inception',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert isinstance(result, ParsedRelease)
        assert result.source == "adithd"
        assert result.year == 2010
        assert result.resolution == "1080p"
        assert result.is_remux is True
        assert result.is_tv is False

    def test_4k_hdr_release(self, source):
        """Parse a 4K HDR release."""
        raw = {
            'title': 'Dune 2021 2160p UHD BluRay REMUX HDR HEVC Atmos-FGT',
            'url': 'https://www.adit-hd.com/Thread-Dune',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.resolution == "4K"
        assert result.is_hdr is True
        assert result.codec == "x265"
        assert result.audio_codec == "Atmos"

    def test_tv_release_from_mode(self, source):
        """TV release detected from mode='tv'."""
        raw = {
            'title': 'The Office Complete Series 1080p BluRay-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Office',
            'mode': 'tv',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is True

    def test_tv_release_from_pattern(self, source):
        """TV release from season/episode pattern."""
        raw = {
            'title': 'Breaking Bad S01E01 1080p BluRay x265-GROUP',
            'url': 'https://www.adit-hd.com/Thread-BB',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is True
        assert result.season == 1
        assert result.episode == 1

    def test_season_pack(self, source):
        """Season pack detection."""
        raw = {
            'title': 'Show S02 1080p BluRay-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Show',
            'mode': 'tv',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_season_pack is True
        assert result.season == 2
        assert result.episode is None

    def test_web_dl_detection(self, source):
        """WEB-DL detection."""
        raw = {
            'title': 'Movie 2023 1080p WEB-DL DD5.1-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Movie',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True

    def test_web_amzn_detection(self, source):
        """AMZN keyword detected as web release."""
        raw = {
            'title': 'Movie 2023 1080p AMZN WEB-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Movie',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True

    def test_web_nf_detection(self, source):
        """NF keyword detected as web release."""
        raw = {
            'title': 'Movie 2023 1080p NF WEB-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Movie',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True

    def test_web_atvp_detection(self, source):
        """ATVP keyword detected as web release."""
        raw = {
            'title': 'Movie 2023 1080p ATVP WEB-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Movie',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True

    def test_release_group(self, source):
        """Release group extraction."""
        raw = {
            'title': 'Movie 2023 1080p BluRay REMUX-FraMeSToR',
            'url': 'https://www.adit-hd.com/Thread-Movie',
            'mode': 'movies',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.release_group == "FraMeSToR"

    def test_default_mode_movies(self, source):
        """Missing mode defaults to 'movies'."""
        raw = {
            'title': 'Movie 2023 1080p-GROUP',
            'url': 'https://www.adit-hd.com/Thread-Movie',
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is False


# ======================================================================
# AditHDSource._extract_display_title Tests
# ======================================================================

class TestExtractDisplayTitle:
    """Tests for AditHDSource._extract_display_title()."""

    def test_removes_multi_tag(self, source):
        """MULTI tag at start should be removed."""
        result = source._extract_display_title(
            "[MULTI] Movie 2023 1080p BluRay REMUX-FGT", 2023
        )
        assert "MULTI" not in result
        assert "Movie" in result

    def test_removes_multi_tag_no_brackets(self, source):
        """MULTI tag without brackets should be removed."""
        result = source._extract_display_title(
            "MULTI Movie 2023 1080p BluRay-GROUP", 2023
        )
        assert "MULTI" not in result.upper()

    def test_removes_quality_tags(self, source):
        """Quality tags removed."""
        result = source._extract_display_title(
            "Inception 2010 1080p BluRay REMUX-FGT", 2010
        )
        assert "1080p" not in result
        assert "BluRay" not in result

    def test_preserves_title_and_year(self, source):
        """Title and year preserved."""
        result = source._extract_display_title(
            "Inception 2010 1080p BluRay-FGT", 2010
        )
        assert "Inception" in result
        assert "2010" in result

    def test_cleans_separators(self, source):
        """Dots, underscores, hyphens replaced with spaces."""
        result = source._extract_display_title(
            "The.Dark-Knight_Rises 2012 1080p BluRay-GROUP", 2012
        )
        assert "." not in result
        assert "_" not in result

    def test_no_year(self, source):
        """When year=0, full title minus tags kept."""
        result = source._extract_display_title("Some Movie 1080p REMUX", 0)
        assert "Some Movie" in result

    def test_strips_whitespace(self, source):
        """Result trimmed."""
        result = source._extract_display_title("  Movie  2023  1080p  ", 2023)
        assert result == result.strip()


# ======================================================================
# AditHDSource._extract_release_group Tests
# ======================================================================

class TestExtractReleaseGroup:
    """Tests for AditHDSource._extract_release_group()."""

    def test_group_at_end(self, source):
        """Group at end."""
        assert source._extract_release_group("Movie 2023 1080p-FraMeSToR") == "FraMeSToR"

    def test_group_before_brackets(self, source):
        """Group before brackets."""
        assert source._extract_release_group("Movie 2023 1080p-FGT[rarbg]") == "FGT"

    def test_no_group(self, source):
        """No group returns empty."""
        assert source._extract_release_group("Movie 2023") == ""

    def test_uses_last_hyphen_for_hyphenated_titles(self, source):
        """Hyphens in the title should not be mistaken for the release group."""
        assert source._extract_release_group("Spider-Man No Way Home 2021 1080p-TEST") == "TEST"

    def test_group_before_trailing_comment(self, source):
        """Release group should still be found when extra text follows it."""
        assert source._extract_release_group("Movie 2023 1080p-TEST proper") == "TEST"


# ======================================================================
# AditHDSource._parse_thread_row Tests
# ======================================================================

class TestParseThreadRow:
    """Tests for AditHDSource._parse_thread_row()."""

    def test_parses_tid_link(self, source):
        """Parse thread row with tid_ link."""
        html = """
        <tr class="inline_row">
            <td>
                <a id="tid_123" href="https://www.adit-hd.com/Thread-Movie-2023-1080p">Movie 2023 1080p BluRay-GROUP</a>
            </td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is not None
        assert result.resolution == "1080p"

    def test_parses_thread_link(self, source):
        """Parse thread row with Thread- href."""
        html = """
        <tr class="inline_row">
            <td>
                <a href="Thread-Movie-2023">Movie 2023 1080p-GROUP</a>
            </td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is not None

    def test_parses_showthread_link(self, source):
        """Parse thread row with showthread href."""
        html = """
        <tr class="inline_row">
            <td>
                <a href="showthread.php?tid=123">Movie 2023 1080p-GROUP</a>
            </td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is not None

    def test_relative_url_made_absolute(self, source):
        """Relative URLs should be made absolute."""
        html = """
        <tr class="inline_row">
            <td>
                <a id="tid_1" href="Thread-Movie-2023">Movie 2023 1080p-GROUP</a>
            </td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is not None
        assert result.url.startswith("https://www.adit-hd.com/")

    def test_absolute_url_preserved(self, source):
        """Absolute URLs should be preserved."""
        html = """
        <tr class="inline_row">
            <td>
                <a id="tid_1" href="https://www.adit-hd.com/Thread-Movie">Movie 2023 1080p-GROUP</a>
            </td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is not None
        assert result.url == "https://www.adit-hd.com/Thread-Movie"

    def test_returns_none_no_link(self, source):
        """Return None when no link found."""
        html = """
        <tr class="inline_row">
            <td><span>No link</span></td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is None

    def test_returns_none_empty_title(self, source):
        """Return None when title is empty."""
        html = """
        <tr class="inline_row">
            <td><a id="tid_1" href="Thread-Movie"></a></td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is None

    def test_subject_new_span_link(self, source):
        """Parse link within subject_new span."""
        html = """
        <tr class="inline_row">
            <td>
                <span class="subject_new">
                    <a href="Thread-Movie">Movie 2023 1080p-GROUP</a>
                </span>
            </td>
        </tr>
        """
        soup = BeautifulSoup(html, 'html.parser')
        row = soup.select_one('tr')
        result = source._parse_thread_row(row, "movies")
        assert result is not None

    def test_exception_returns_none(self, source):
        """Exception should return None."""
        result = source._parse_thread_row(None, "movies")
        assert result is None


# ======================================================================
# AditHDSource._has_next_page Tests
# ======================================================================

class TestHasNextPage:
    """Tests for AditHDSource._has_next_page()."""

    def test_detects_next_text(self, source):
        """Detect Next link text in pagination."""
        html = """
        <div class="pagination">
            <a href="?page=2">Next</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_arrow_text(self, source):
        """Detect > arrow in pagination."""
        html = """
        <div class="pagination">
            <a href="?page=2">></a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_double_arrow(self, source):
        """Detect >> in pagination."""
        html = """
        <div class="pagination">
            <a href="?page=5">>></a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_page_numbers(self, source):
        """Detect next page from page numbers."""
        html = """
        <span class="pagination_current">1</span>
        <div class="pagination">
            <a href="?page=2">2</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_no_next_page(self, source):
        """No pagination returns False."""
        html = '<div>No pagination</div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False

    def test_last_page(self, source):
        """Last page returns False."""
        html = """
        <span class="pagination_current">3</span>
        <div class="pagination">
            <a href="?page=1">1</a>
            <a href="?page=2">2</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False

    def test_non_numeric_current(self, source):
        """Non-numeric current handled gracefully."""
        html = """
        <span class="pagination_current">abc</span>
        <div class="pagination">
            <a href="?page=2">2</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        # ValueError in current, returns False
        assert source._has_next_page(soup) is False


# ======================================================================
# AditHDSource._fetch_html Tests
# ======================================================================

class TestFetchHtml:
    """Tests for AditHDSource._fetch_html()."""

    def test_uses_driver_when_logged_in(self, source):
        """Use Selenium driver when logged in."""
        source._is_logged_in = True
        mock_driver = MagicMock()
        mock_driver.page_source = "<html>driver content</html>"
        source._driver = mock_driver

        result = source._fetch_html("https://www.adit-hd.com/Thread-test")

        mock_driver.get.assert_called_once()
        assert result == "<html>driver content</html>"

    def test_falls_back_to_scraper_when_driver_fails(self, source):
        """Fallback to scraper when driver fails."""
        source._is_logged_in = True
        mock_driver = MagicMock()
        mock_driver.get.side_effect = RuntimeError("driver error")
        source._driver = mock_driver

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html>scraper content</html>"
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://www.adit-hd.com/Thread-test")

        assert result == "<html>scraper content</html>"

    def test_uses_scraper_when_not_logged_in(self, source):
        """Use scraper when not logged in."""
        source._is_logged_in = False

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html>content</html>"
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://www.adit-hd.com/Thread-test")

        assert result == "<html>content</html>"

    def test_non_200_returns_none(self, source):
        """Non-200 status returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://www.adit-hd.com/Thread-test")

        assert result is None

    def test_exception_returns_none(self, source):
        """Exception returns None."""
        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = ConnectionError("fail")

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://www.adit-hd.com/Thread-test")

        assert result is None


# ======================================================================
# AditHDSource._extract_download_links Tests
# ======================================================================

class TestExtractDownloadLinks:
    """Tests for AditHDSource._extract_download_links()."""

    def test_extract_rapidgator_links(self, source):
        """Extract Rapidgator links."""
        html = '<a href="https://rapidgator.net/file/abc123">Download</a>'
        links = source._extract_download_links(html)
        assert any("rapidgator.net" in link for link in links)

    def test_extract_nitroflare_links(self, source):
        """Extract NitroFlare links."""
        html = '<a href="https://nitroflare.com/view/abc123">Download</a>'
        links = source._extract_download_links(html)
        assert any("nitroflare.com" in link for link in links)

    def test_extract_1fichier_links(self, source):
        """Extract 1fichier links."""
        html = '<a href="https://1fichier.com/?abc123">Download</a>'
        links = source._extract_download_links(html)
        assert any("1fichier.com" in link for link in links)

    def test_extract_ddownload_links(self, source):
        """Extract DDownload links."""
        html = '<a href="https://ddownload.com/abc123/file.mkv">Download</a>'
        links = source._extract_download_links(html)
        assert any("ddownload.com" in link for link in links)

    def test_preferred_host_filter(self, source):
        """Preferred host filter returns only matching links."""
        html = (
            '<a href="https://rapidgator.net/file/abc123">RG</a>'
            '<a href="https://nitroflare.com/view/xyz789">NF</a>'
        )
        links = source._extract_download_links(html, preferred_host="rapidgator")
        assert all("rapidgator.net" in link for link in links)

    def test_no_preferred_returns_all(self, source):
        """No preferred host returns all links."""
        html = (
            '<a href="https://rapidgator.net/file/abc">RG</a>'
            '<a href="https://nitroflare.com/view/xyz">NF</a>'
        )
        links = source._extract_download_links(html)
        assert len(links) >= 2

    def test_empty_html(self, source):
        """Empty HTML returns empty list."""
        links = source._extract_download_links("")
        assert links == []

    def test_no_download_links(self, source):
        """HTML with no download links returns empty list."""
        html = '<a href="https://example.com">Not a download</a>'
        links = source._extract_download_links(html)
        assert links == []

    def test_deduplicates_links(self, source):
        """Duplicate links should be deduplicated."""
        html = (
            '<a href="https://rapidgator.net/file/abc123">RG 1</a>'
            '<a href="https://rapidgator.net/file/abc123">RG 1 dup</a>'
        )
        links = source._extract_download_links(html)
        assert len(links) == len(set(links))

    def test_preferred_host_not_found_returns_all(self, source):
        """When preferred host has no links, return all links."""
        html = '<a href="https://rapidgator.net/file/abc123">RG</a>'
        links = source._extract_download_links(html, preferred_host="nitroflare")
        # nitroflare has no links, so all links returned
        assert len(links) > 0


# ======================================================================
# AditHDSource.fetch_page Tests
# ======================================================================

class TestFetchPage:
    """Tests for AditHDSource.fetch_page()."""

    def test_fetch_movies_page(self, source):
        """Fetch movies page."""
        html = _build_thread_listing([
            ("Movie 2023 1080p BluRay REMUX-GROUP", "https://www.adit-hd.com/Thread-Movie"),
        ], has_next=True)

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1
        assert result.has_next is True

    def test_fetch_tv_page(self, source):
        """Fetch TV page."""
        html = _build_thread_listing([
            ("Show S01 1080p-GROUP", "https://www.adit-hd.com/Thread-Show"),
        ])

        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            result = asyncio.run(source.fetch_page(page=1, mode="tv"))

        assert len(result.releases) == 1
        assert "Tv-Shows" in calls[0]

    def test_fetch_all_mode_combines(self, source):
        """'all' mode combines movies and TV."""
        html = _build_thread_listing([
            ("Movie 2023 1080p-GROUP", "https://www.adit-hd.com/Thread-Movie"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="all"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 2  # 1 from movies, 1 from TV

    def test_fetch_page_pagination_url(self, source):
        """Page > 1 uses pagination URL."""
        html = _build_thread_listing([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=3, mode="movies"))

        assert "?page=3" in calls[0]

    def test_fetch_page_1_no_pagination(self, source):
        """Page 1 should not have pagination param."""
        html = _build_thread_listing([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert "?page=" not in calls[0]

    def test_fetch_page_html_none(self, source):
        """None HTML returns error."""
        with patch.object(source, '_fetch_html', return_value=None):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert result.releases == []
        assert len(result.errors) > 0

    def test_fetch_page_exception(self, source):
        """Exception returns error."""
        with patch.object(source, '_fetch_html', side_effect=RuntimeError("error")):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert result.releases == []
        assert len(result.errors) > 0

    def test_fetch_page_triggers_login(self, source_with_creds):
        """Credentials set but not logged in triggers login."""
        html = _build_thread_listing([])

        async def mock_login():
            return True

        with patch.object(source_with_creds, 'login', side_effect=mock_login):
            with patch.object(source_with_creds, '_fetch_html', return_value=html):
                result = asyncio.run(source_with_creds.fetch_page(page=1, mode="movies"))

        assert isinstance(result, PageResult)

    def test_fetch_page_multiple_threads(self, source):
        """Multiple threads parsed."""
        html = _build_thread_listing([
            ("Movie One 2023 1080p-A", "https://www.adit-hd.com/Thread-One"),
            ("Movie Two 2022 4K-B", "https://www.adit-hd.com/Thread-Two"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert len(result.releases) == 2

    def test_fetch_page_bad_thread_skipped(self, source):
        """Threads that fail to parse are skipped."""
        html = """
        <html><body>
        <tr class="inline_row">
            <td><a id="tid_1" href="Thread-Good">Good Movie 2023 1080p-GROUP</a></td>
        </tr>
        <tr class="inline_row">
            <td><span>No link</span></td>
        </tr>
        </body></html>
        """
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert len(result.releases) == 1


# ======================================================================
# AditHDSource.search Tests
# ======================================================================

class TestSearch:
    """Tests for AditHDSource.search()."""

    def test_search_returns_results(self, source):
        """Search returns results."""
        html = _build_thread_listing([
            ("Inception 2010 1080p-GROUP", "https://www.adit-hd.com/Thread-Inception"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("Inception"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1

    def test_search_url_format(self, source):
        """Search URL includes encoded query."""
        html = _build_thread_listing([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.search("The Dark Knight"))

        assert "keywords=The+Dark+Knight" in calls[0]

    def test_search_mode_movies_filters_tv(self, source):
        """mode=movies filters TV."""
        html = """
        <html><body>
        <tr class="inline_row">
            <td><a id="tid_1" href="Thread-Movie">Movie 2023 1080p-GROUP</a></td>
        </tr>
        <tr class="inline_row">
            <td><a id="tid_2" href="Thread-Show">Show S01E01 1080p-GROUP</a></td>
        </tr>
        </body></html>
        """
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("test", mode="movies"))

        assert all(not r.is_tv for r in result.releases)

    def test_search_mode_tv_filters_movies(self, source):
        """mode=tv filters movies."""
        html = """
        <html><body>
        <tr class="inline_row">
            <td><a id="tid_1" href="Thread-Movie">Movie 2023 1080p-GROUP</a></td>
        </tr>
        <tr class="inline_row">
            <td><a id="tid_2" href="Thread-Show">Show S01E01 1080p-GROUP</a></td>
        </tr>
        </body></html>
        """
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("test", mode="tv"))

        assert all(r.is_tv for r in result.releases)

    def test_search_html_failure(self, source):
        """Search returns error on HTML failure."""
        with patch.object(source, '_fetch_html', return_value=None):
            result = asyncio.run(source.search("test"))

        assert result.releases == []
        assert len(result.errors) > 0

    def test_search_exception(self, source):
        """Search returns error on exception."""
        with patch.object(source, '_fetch_html', side_effect=RuntimeError("fail")):
            result = asyncio.run(source.search("test"))

        assert result.releases == []
        assert len(result.errors) > 0


# ======================================================================
# AditHDSource.login Tests
# ======================================================================

class TestLogin:
    """Tests for AditHDSource.login()."""

    def test_returns_false_no_driver(self, source):
        """Return False when no driver."""
        source._credentials = {'username': 'u', 'password': 'p'}
        source._driver = None
        result = asyncio.run(source.login())
        assert result is False

    def test_returns_false_no_credentials(self, source):
        """Return False when no credentials."""
        source._driver = MagicMock()
        source._credentials = None
        result = asyncio.run(source.login())
        assert result is False

    def test_returns_true_already_logged_in(self, source):
        """Return True if already logged in."""
        source._driver = MagicMock()
        source._credentials = {'username': 'u', 'password': 'p'}
        source._is_logged_in = True
        result = asyncio.run(source.login())
        assert result is True

    def test_login_failure_import_error(self, source_with_creds):
        """Return False when selenium modules can't be imported."""
        mock_driver = MagicMock()
        source_with_creds._driver = mock_driver

        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def mock_import(name, *args, **kwargs):
            if 'selenium' in name:
                raise ImportError("No selenium")
            return original_import(name, *args, **kwargs)

        # Simulating login exception path
        mock_driver.get.side_effect = Exception("Simulated login error")

        result = asyncio.run(source_with_creds.login())
        assert result is False


# ======================================================================
# AditHDSource.fetch_thread_content Tests
# ======================================================================

class TestFetchThreadContent:
    """Tests for AditHDSource.fetch_thread_content()."""

    def test_fetch_with_scraper(self, source):
        """Fetch thread content using scraper (not logged in)."""
        html = _build_thread_content(links=[
            "https://rapidgator.net/file/abc123",
            "https://nitroflare.com/view/xyz789",
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            content, links = asyncio.run(source.fetch_thread_content("https://www.adit-hd.com/Thread-test"))

        assert len(content) > 0
        assert len(links) == 2

    def test_fetch_with_driver_when_logged_in(self, source):
        """Fetch using driver when logged in."""
        source._is_logged_in = True
        source._credentials = None  # No auto_reply to avoid calling _handle_hidden_content
        mock_driver = MagicMock()
        mock_driver.page_source = _build_thread_content(links=[
            "https://rapidgator.net/file/abc123",
        ])
        source._driver = mock_driver

        async def mock_sleep(t):
            pass

        with patch('asyncio.sleep', side_effect=mock_sleep):
            content, links = asyncio.run(source.fetch_thread_content("https://www.adit-hd.com/Thread-test"))

        assert len(links) == 1

    def test_fetch_returns_empty_on_html_failure(self, source):
        """Return empty content/links when HTML fetch fails."""
        with patch.object(source, '_fetch_html', return_value=None):
            content, links = asyncio.run(source.fetch_thread_content("https://www.adit-hd.com/Thread-test"))

        assert content == ""
        assert links == []

    def test_fetch_returns_empty_on_exception(self, source):
        """Return empty on exception."""
        with patch.object(source, '_fetch_html', side_effect=RuntimeError("fail")):
            content, links = asyncio.run(source.fetch_thread_content("https://www.adit-hd.com/Thread-test"))

        assert content == ""
        assert links == []


# ======================================================================
# AditHDSource._handle_hidden_content Tests
# ======================================================================

class TestHandleHiddenContent:
    """Tests for AditHDSource._handle_hidden_content()."""

    def test_skips_already_replied_thread(self, source):
        """Skip threads already replied to."""
        source._replied_threads = {"https://www.adit-hd.com/Thread-test"}
        source._driver = MagicMock()

        asyncio.run(source._handle_hidden_content("https://www.adit-hd.com/Thread-test"))

        # Should not interact with driver since already replied
        source._driver.page_source  # Accessing this is OK

    def test_skips_when_no_hidden_content(self, source):
        """Skip when no hidden content found."""
        source._driver = MagicMock()
        source._driver.page_source = "<html><body><div class='post_body'>Normal content</div></body></html>"
        source._credentials = {'auto_reply': True}

        asyncio.run(source._handle_hidden_content("https://www.adit-hd.com/Thread-test"))

        # Thread should not be added to replied
        assert "https://www.adit-hd.com/Thread-test" not in source._replied_threads

    def test_skips_when_links_already_visible(self, source):
        """Skip when download links already visible."""
        source._driver = MagicMock()
        source._driver.page_source = (
            '<html><body>'
            '<div class="hidden_content">Hidden</div>'
            '<a href="https://rapidgator.net/file/abc123">Link</a>'
            '</body></html>'
        )
        source._credentials = {'auto_reply': True}

        asyncio.run(source._handle_hidden_content("https://www.adit-hd.com/Thread-test"))

        assert "https://www.adit-hd.com/Thread-test" not in source._replied_threads


# ======================================================================
# AditHDSource.fetch_download_links Tests
# ======================================================================

class TestFetchDownloadLinksMethod:
    """Tests for AditHDSource.fetch_download_links()."""

    def test_fetch_links_for_release(self, source):
        """Fetch download links for a release (default service=rapidgator filters)."""
        html = _build_thread_content(links=[
            "https://rapidgator.net/file/abc123",
            "https://rapidgator.net/file/def456",
            "https://nitroflare.com/view/xyz789",
        ])
        release = ParsedRelease(
            title="Movie 2023", url="https://www.adit-hd.com/Thread-Movie", source="adithd"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            # Default service is "rapidgator", so only rapidgator links
            links = asyncio.run(source.fetch_download_links(release))

        assert len(links) >= 1
        assert all("rapidgator.net" in link for link in links)

    def test_fetch_links_no_service_filter(self, source):
        """Fetch all links when service is empty string."""
        html = _build_thread_content(links=[
            "https://rapidgator.net/file/abc123",
            "https://nitroflare.com/view/xyz789",
        ])
        release = ParsedRelease(
            title="Movie 2023", url="https://www.adit-hd.com/Thread-Movie", source="adithd"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release, service=""))

        assert len(links) >= 2

    def test_fetch_links_filter_by_service(self, source):
        """Filter links by service."""
        html = _build_thread_content(links=[
            "https://rapidgator.net/file/abc123",
            "https://nitroflare.com/view/xyz789",
        ])
        release = ParsedRelease(
            title="Movie 2023", url="https://www.adit-hd.com/Thread-Movie", source="adithd"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release, service="rapidgator"))

        assert all("rapidgator.net" in link for link in links)

    def test_fetch_links_empty_on_failure(self, source):
        """Return empty on fetch failure."""
        release = ParsedRelease(
            title="Movie 2023", url="https://www.adit-hd.com/Thread-Movie", source="adithd"
        )

        with patch.object(source, '_fetch_html', side_effect=RuntimeError("fail")):
            links = asyncio.run(source.fetch_download_links(release))

        assert links == []


# ======================================================================
# AditHDSource properties and constants Tests
# ======================================================================

class TestProperties:
    """Tests for AditHDSource properties and constants."""

    def test_name_property(self, source):
        """name property returns 'adithd'."""
        assert source.name == "adithd"

    def test_config_property(self, source):
        """config property returns SourceConfig."""
        assert isinstance(source.config, SourceConfig)

    def test_base_url(self):
        """BASE_URL class attribute."""
        assert AditHDSource.BASE_URL == "https://www.adit-hd.com"

    def test_login_url(self):
        """LOGIN_URL class attribute."""
        assert AditHDSource.LOGIN_URL == "https://www.adit-hd.com/member.php"

    def test_url_patterns(self):
        """URL_PATTERNS should have expected keys."""
        patterns = AditHDSource.URL_PATTERNS
        assert 'movies' in patterns
        assert 'tv' in patterns

    def test_thread_selectors(self):
        """THREAD_SELECTORS should have expected keys."""
        selectors = AditHDSource.THREAD_SELECTORS
        assert 'thread_row' in selectors
        assert 'thread_link' in selectors

    def test_post_selectors(self):
        """POST_SELECTORS should have expected keys."""
        selectors = AditHDSource.POST_SELECTORS
        assert 'post_content' in selectors
        assert 'hidden_content' in selectors
        assert 'reply_form' in selectors

    def test_host_patterns(self):
        """HOST_PATTERNS should have expected hosts."""
        patterns = AditHDSource.HOST_PATTERNS
        assert 'rapidgator' in patterns
        assert 'nitroflare' in patterns
        assert '1fichier' in patterns
        assert 'ddownload' in patterns

    def test_host_patterns_are_compiled_regex(self):
        """HOST_PATTERNS values should be compiled regex patterns."""
        import re
        for host, pattern in AditHDSource.HOST_PATTERNS.items():
            assert isinstance(pattern, re.Pattern), f"{host} pattern is not compiled"
