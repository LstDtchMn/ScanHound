"""Comprehensive tests for backend/sources/hdencode.py module.

Covers:
- HDEncodeSource.get_config: returns correct SourceConfig
- HDEncodeSource.__init__: initialization
- HDEncodeSource.parse_release: parsing logic for movie/TV titles
- HDEncodeSource._extract_display_title: title cleaning
- HDEncodeSource._extract_release_group: group extraction
- HDEncodeSource._parse_article: article HTML element parsing
- HDEncodeSource._has_next_page: pagination detection
- HDEncodeSource._fetch_html: HTML fetching with scraper
- HDEncodeSource.fetch_page: async page fetching with mocked HTTP
- HDEncodeSource.search: search functionality
- HDEncodeSource.fetch_release_details: detail page fetching
- HDEncodeSource.fetch_download_links: Selenium-based link extraction
- Edge cases and error handling
"""

import asyncio
import os
import sys
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.sources.hdencode import HDEncodeSource
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
    """Provide an HDEncodeSource instance with mocked scraper."""
    with patch.object(HDEncodeSource, '_get_scraper', return_value=MagicMock()):
        src = HDEncodeSource()
    return src


# ---------------------------------------------------------------------------
# Sample HTML builders
# ---------------------------------------------------------------------------

def _build_listing_page(articles, has_next=False):
    """Build a minimal HDEncode listing page HTML."""
    article_html = ""
    for title, url in articles:
        article_html += f"""
        <article class="post type-post">
            <h2><a href="{url}">{title}</a></h2>
            <div class="entry-content">
                <p>Some content. Size: 25.3 GB</p>
            </div>
        </article>
        """
    pagination = ""
    if has_next:
        pagination = '<a class="next" href="/category/movies/page/2/">Next</a>'

    return f"""
    <html><body>
    <div class="posts">
        {article_html}
    </div>
    <div class="pagination">
        {pagination}
    </div>
    </body></html>
    """


def _build_detail_page(title, content_html="", imdb_url=None, screenshots=None, description=None):
    """Build a minimal HDEncode detail page."""
    imdb_link = ""
    if imdb_url:
        imdb_link = f'<a href="{imdb_url}">IMDb</a>'

    screenshot_html = ""
    if screenshots:
        for url in screenshots:
            screenshot_html += f'<img src="{url}" />'

    desc_html = ""
    if description:
        desc_html = f'<p class="description">{description}</p>'

    return f"""
    <html><body>
    <h1 class="entry-title">{title}</h1>
    <div class="entry-content">
        {desc_html}
        {content_html}
        {screenshot_html}
        {imdb_link}
    </div>
    </body></html>
    """


def _build_search_results(results):
    """Build a search results page matching HDEncode's actual search layout."""
    items = ""
    for title, url, is_tv in results:
        items += f"""
        <div class="fit item">
            <div class="data">
                <h5><a href="{url}">{title}</a></h5>
            </div>
        </div>
        """
    return f"<html><body><div class='peliculas'>{items}</div></body></html>"


# ======================================================================
# HDEncodeSource.get_config Tests
# ======================================================================

class TestGetConfig:
    """Tests for HDEncodeSource.get_config()."""

    def test_returns_source_config(self):
        """get_config should return a SourceConfig instance."""
        config = HDEncodeSource.get_config()
        assert isinstance(config, SourceConfig)

    def test_name(self):
        """Config name should be 'hdencode'."""
        config = HDEncodeSource.get_config()
        assert config.name == "hdencode"

    def test_display_name(self):
        """Config display_name should be 'HDEncode'."""
        config = HDEncodeSource.get_config()
        assert config.display_name == "HDEncode"

    def test_base_url(self):
        """Config base_url should be the HDEncode URL."""
        config = HDEncodeSource.get_config()
        assert config.base_url == "https://hdencode.org"

    def test_capabilities_include_movies(self):
        """Capabilities should include MOVIES."""
        config = HDEncodeSource.get_config()
        assert SourceCapability.MOVIES in config.capabilities

    def test_capabilities_include_tv_shows(self):
        """Capabilities should include TV_SHOWS."""
        config = HDEncodeSource.get_config()
        assert SourceCapability.TV_SHOWS in config.capabilities

    def test_capabilities_include_pagination(self):
        """Capabilities should include PAGINATION."""
        config = HDEncodeSource.get_config()
        assert SourceCapability.PAGINATION in config.capabilities

    def test_capabilities_include_search(self):
        """Capabilities should include SEARCH."""
        config = HDEncodeSource.get_config()
        assert SourceCapability.SEARCH in config.capabilities

    def test_capabilities_exclude_direct_links(self):
        """HDEncode's direct-link method is fail-closed, so the plugin must
        not claim DIRECT_LINKS (source capability correction, RSS actions
        package)."""
        config = HDEncodeSource.get_config()
        assert not (config.capabilities & SourceCapability.DIRECT_LINKS)

    def test_capabilities_exclude_cloudflare_bypass(self):
        """HDEncode RSS is fetched via the DNS-pinned client, so the plugin
        must not claim CLOUDFLARE_BYPASS (source capability correction)."""
        config = HDEncodeSource.get_config()
        assert not (config.capabilities & SourceCapability.CLOUDFLARE_BYPASS)

    def test_does_not_require_cloudflare_bypass(self):
        """HDEncode does not require cloudflare bypass (source capability
        correction; RSS uses the pinned direct-IP client)."""
        config = HDEncodeSource.get_config()
        assert config.requires_cloudflare_bypass is False

    def test_enabled_by_default(self):
        """HDEncode should be enabled by default."""
        config = HDEncodeSource.get_config()
        assert config.enabled is True

    def test_priority(self):
        """HDEncode should have priority 100."""
        config = HDEncodeSource.get_config()
        assert config.priority == 100

    def test_timeout(self):
        """HDEncode timeout should be 30."""
        config = HDEncodeSource.get_config()
        assert config.timeout == 30

    def test_rate_limit(self):
        """HDEncode rate_limit should be 2.0."""
        config = HDEncodeSource.get_config()
        assert config.rate_limit == 2.0


# ======================================================================
# HDEncodeSource.__init__ Tests
# ======================================================================

class TestInit:
    """Tests for HDEncodeSource.__init__()."""

    def test_session_is_none(self, source):
        """_session should be None initially."""
        assert source._session is None

    def test_config_set(self, source):
        """Config should be set after init."""
        assert source._config is not None
        assert source._config.name == "hdencode"


# ======================================================================
# HDEncodeSource.parse_release Tests
# ======================================================================

class TestParseRelease:
    """Tests for HDEncodeSource.parse_release()."""

    def test_returns_none_for_non_dict(self, source):
        """Non-dict raw_data should return None."""
        assert source.parse_release("not a dict") is None
        assert source.parse_release(123) is None
        assert source.parse_release(None) is None

    def test_returns_none_for_empty_title(self, source):
        """Empty title should return None."""
        result = source.parse_release({'title': '', 'url': 'http://example.com'})
        assert result is None

    def test_basic_movie_release(self, source):
        """Parse a basic movie release title."""
        raw = {
            'title': 'Inception 2010 1080p BluRay REMUX AVC DTS-HD MA 5.1-FGT',
            'url': 'https://hdencode.org/inception/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert isinstance(result, ParsedRelease)
        assert result.source == "hdencode"
        assert result.year == 2010
        assert result.resolution == "1080p"
        assert result.is_remux is True
        assert result.is_tv is False

    def test_4k_release(self, source):
        """Parse a 4K release."""
        raw = {
            'title': 'Inception 2010 2160p UHD BluRay REMUX HEVC DTS-X-FGT',
            'url': 'https://hdencode.org/inception/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.resolution == "4K"
        assert result.codec == "x265"

    def test_tv_release_from_mode(self, source):
        """TV release detected from mode='tv'."""
        raw = {
            'title': 'The Office Complete Series 1080p BluRay-GROUP',
            'url': 'https://hdencode.org/the-office/',
            'mode': 'tv',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is True

    def test_tv_release_from_season_pattern(self, source):
        """TV release detected from S01E01 pattern in title."""
        raw = {
            'title': 'Breaking Bad S01E01 1080p BluRay x265-GROUP',
            'url': 'https://hdencode.org/breaking-bad/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is True
        assert result.season == 1
        assert result.episode == 1

    def test_season_pack_detection(self, source):
        """Season pack is detected when season exists but no episode."""
        raw = {
            'title': 'Breaking Bad S01 1080p BluRay x265-GROUP',
            'url': 'https://hdencode.org/breaking-bad-s01/',
            'mode': 'tv',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is True
        assert result.season == 1
        assert result.episode is None
        assert result.is_season_pack is True

    def test_not_season_pack_when_episode_present(self, source):
        """Not a season pack when episode is present."""
        raw = {
            'title': 'Show S02E05 1080p-GROUP',
            'url': 'https://hdencode.org/show/',
            'mode': 'tv',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_season_pack is False
        assert result.season == 2
        assert result.episode == 5

    def test_web_dl_detection(self, source):
        """WEB-DL releases should be detected."""
        raw = {
            'title': 'Movie 2023 1080p WEB-DL DD5.1 H264-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True
        assert result.is_remux is False

    def test_webdl_no_hyphen_detection(self, source):
        """WEBDL (no hyphen) should also be detected as web."""
        raw = {
            'title': 'Movie 2023 1080p WEBDL-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True

    def test_webrip_detection(self, source):
        """WEBRip should be detected as web."""
        raw = {
            'title': 'Movie 2023 1080p WEBRip x264-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True

    def test_hdr10_detection(self, source):
        """HDR10 should be detected."""
        raw = {
            'title': 'Movie 2023 2160p UHD BluRay REMUX HDR10 HEVC-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_hdr is True

    def test_size_extraction_from_article_html(self, source):
        """Size should be extracted from article HTML."""
        raw = {
            'title': 'Movie 2023 1080p REMUX-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': '<div>Size: 42.1 GB</div>'
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.size == "42.1 GB"
        assert result.size_bytes > 0

    def test_imdb_from_article_html(self, source):
        """IMDb ID extracted from article HTML."""
        raw = {
            'title': 'Movie 2023 1080p-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': '<a href="https://imdb.com/title/tt9876543/">IMDb</a>'
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.imdb_id == "tt9876543"

    def test_no_imdb_when_no_article_html(self, source):
        """No IMDb when article_html is empty."""
        raw = {
            'title': 'Movie 2023 1080p-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.imdb_id is None

    def test_release_group_extraction(self, source):
        """Release group from title end."""
        raw = {
            'title': 'Movie 2023 1080p BluRay REMUX-FraMeSToR',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.release_group == "FraMeSToR"

    def test_raw_data_preserved(self, source):
        """Raw data should be stored."""
        raw = {
            'title': 'Movie 2023 1080p-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.raw_data == raw

    def test_default_mode_is_movies(self, source):
        """Missing mode defaults to 'movies'."""
        raw = {
            'title': 'Movie 2023 1080p-GROUP',
            'url': 'https://hdencode.org/movie/',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_tv is False

    def test_codec_h264_normalized(self, source):
        """H.264 should normalize to x264."""
        raw = {
            'title': 'Movie 2023 1080p WEB-DL H.264-GROUP',
            'url': 'https://hdencode.org/movie/',
            'mode': 'movies',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.codec == "x264"


# ======================================================================
# HDEncodeSource._extract_display_title Tests
# ======================================================================

class TestExtractDisplayTitle:
    """Tests for HDEncodeSource._extract_display_title()."""

    def test_removes_tags_after_year(self, source):
        """Tags after year should be removed."""
        result = source._extract_display_title(
            "Inception 2010 1080p BluRay REMUX-FGT", 2010
        )
        assert "1080p" not in result
        assert "BluRay" not in result
        assert "Inception" in result
        assert "2010" in result

    def test_cleans_dots(self, source):
        """Dots should be replaced with spaces."""
        result = source._extract_display_title(
            "The.Dark.Knight.2008.1080p.BluRay-GROUP", 2008
        )
        assert "." not in result
        assert "The" in result

    def test_no_year(self, source):
        """When year=0, full title minus tags should be kept."""
        result = source._extract_display_title("Some Movie 1080p REMUX", 0)
        assert "Some Movie" in result

    def test_strips_whitespace(self, source):
        """Result should be trimmed."""
        result = source._extract_display_title("  Movie  2023  1080p  ", 2023)
        assert result == result.strip()


# ======================================================================
# HDEncodeSource._extract_release_group Tests
# ======================================================================

class TestExtractReleaseGroup:
    """Tests for HDEncodeSource._extract_release_group()."""

    def test_group_at_end(self, source):
        """Group at end of title."""
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
# HDEncodeSource._parse_article Tests
# ======================================================================

class TestParseArticle:
    """Tests for HDEncodeSource._parse_article()."""

    def test_parses_h2_a(self, source):
        """Parse article with h2 > a link."""
        html = """
        <article class="post type-post">
            <h2><a href="https://hdencode.org/movie/">Movie 2023 1080p BluRay-GROUP</a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "movies")
        assert result is not None
        assert result.resolution == "1080p"

    def test_parses_entry_title(self, source):
        """Parse article with .entry-title a link."""
        html = """
        <article class="post type-post">
            <div class="entry-title"><a href="https://hdencode.org/movie/">Movie 2023 4K UHD-GROUP</a></div>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "movies")
        assert result is not None

    def test_returns_none_no_link(self, source):
        """Return None when no title link."""
        html = """
        <article class="post type-post">
            <div>No link here</div>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "movies")
        assert result is None

    def test_returns_none_empty_title(self, source):
        """Return None when title text is empty."""
        html = """
        <article class="post type-post">
            <h2><a href="https://hdencode.org/movie/"></a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "movies")
        assert result is None

    def test_returns_none_empty_href(self, source):
        """Return None when href is empty."""
        html = """
        <article class="post type-post">
            <h2><a href="">Movie Title</a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "movies")
        assert result is None

    def test_exception_returns_none(self, source):
        """Exception should return None."""
        result = source._parse_article(None, "movies")
        assert result is None

    def test_mode_passed_to_parse_release(self, source):
        """Mode should be passed through to parse_release."""
        html = """
        <article class="post type-post">
            <h2><a href="https://hdencode.org/show/">Show S01 1080p-GROUP</a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "tv")
        assert result is not None
        assert result.is_tv is True


# ======================================================================
# HDEncodeSource._has_next_page Tests
# ======================================================================

class TestHasNextPage:
    """Tests for HDEncodeSource._has_next_page()."""

    def test_detects_next_link(self, source):
        """Detect next page via a.next."""
        html = '<div><a class="next" href="/page/2/">Next</a></div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_nav_next(self, source):
        """Detect next page via .nav-next a."""
        html = '<div class="nav-next"><a href="/page/2/">Next</a></div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_page_numbers(self, source):
        """Detect next page from page numbers."""
        html = """
        <div class="pagination">
            <span class="page-numbers current">1</span>
            <a class="page-numbers" href="/page/2/">2</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_no_next_page(self, source):
        """No pagination returns False."""
        html = '<div>No pagination</div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False

    def test_last_page_no_next(self, source):
        """Last page returns False."""
        html = """
        <div class="pagination">
            <a class="page-numbers" href="/page/1/">1</a>
            <span class="page-numbers current">2</span>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False

    def test_non_numeric_current(self, source):
        """Non-numeric current page handled gracefully."""
        html = """
        <div class="pagination">
            <span class="page-numbers current">...</span>
            <a class="page-numbers" href="/page/2/">2</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False


# ======================================================================
# HDEncodeSource._fetch_html Tests
# ======================================================================

class TestFetchHtml:
    """Tests for HDEncodeSource._fetch_html()."""

    def test_successful_fetch(self, source):
        """Return HTML on 200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html>content</html>"
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://hdencode.org/test/")
        assert result == "<html>content</html>"

    def test_non_200_returns_none(self, source):
        """Non-200 returns None."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://hdencode.org/test/")
        assert result is None

    def test_exception_returns_none(self, source):
        """Exception returns None."""
        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = ConnectionError("fail")

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://hdencode.org/test/")
        assert result is None


# ======================================================================
# HDEncodeSource.fetch_page Tests
# ======================================================================

class TestFetchPage:
    """Tests for HDEncodeSource.fetch_page()."""

    def test_fetch_movies_page(self, source):
        """Fetch movies page."""
        html = _build_listing_page([
            ("Movie 2023 1080p BluRay REMUX-GROUP", "https://hdencode.org/movie/"),
        ], has_next=True)

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1
        assert result.has_next is True

    def test_fetch_movies_1080p(self, source):
        """Fetch movies with 1080p resolution."""
        html = _build_listing_page([
            ("Movie 2023 1080p-GROUP", "https://hdencode.org/movie/"),
        ])

        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        assert len(result.releases) == 1
        assert "/1080p/" in calls[0]

    def test_fetch_movies_4k(self, source):
        """Fetch movies with 4K resolution."""
        html = _build_listing_page([
            ("Movie 2023 2160p UHD-GROUP", "https://hdencode.org/movie/"),
        ])

        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="4K"))

        assert len(result.releases) == 1
        assert "/4k-uhd/" in calls[0]

    def test_fetch_tv_page(self, source):
        """Fetch TV shows page."""
        html = _build_listing_page([
            ("Show S01E01 1080p-GROUP", "https://hdencode.org/show/"),
        ])

        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            result = asyncio.run(source.fetch_page(page=1, mode="tv"))

        assert len(result.releases) == 1
        assert "/tv-shows/" in calls[0]

    def test_fetch_tv_1080p(self, source):
        """Fetch TV 1080p page."""
        html = _build_listing_page([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=1, mode="tv", resolution="1080p"))

        assert "/1080p-tv-shows/" in calls[0]

    def test_fetch_tv_4k(self, source):
        """Fetch TV 4K page."""
        html = _build_listing_page([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=1, mode="tv", resolution="4K"))

        assert "/4k-uhd-tv-shows/" in calls[0]

    def test_fetch_all_mode_combines(self, source):
        """'all' mode combines movies and TV."""
        html = _build_listing_page([
            ("Movie 2023 1080p-GROUP", "https://hdencode.org/movie/"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="all"))

        assert isinstance(result, PageResult)
        # movies + tv each return 1 release
        assert len(result.releases) == 2

    def test_fetch_page_pagination_url(self, source):
        """Page > 1 uses pagination URL."""
        html = _build_listing_page([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=5, mode="movies"))

        assert "/page/5/" in calls[0]

    def test_fetch_page_1_no_page_suffix(self, source):
        """Page 1 does not use /page/1/ suffix."""
        html = _build_listing_page([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert "/page/" not in calls[0]

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

    def test_fetch_page_multiple_articles(self, source):
        """Multiple articles parsed correctly."""
        html = _build_listing_page([
            ("Movie One 2023 1080p-A", "https://hdencode.org/one/"),
            ("Movie Two 2022 4K-B", "https://hdencode.org/two/"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert len(result.releases) == 2

    def test_fetch_page_bad_article_skipped(self, source):
        """Articles that fail to parse are skipped."""
        html = """
        <html><body>
        <article class="post type-post">
            <h2><a href="https://hdencode.org/good/">Good Movie 2023 1080p-GROUP</a></h2>
        </article>
        <article class="post type-post">
            <div>No link</div>
        </article>
        </body></html>
        """
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert len(result.releases) == 1


# ======================================================================
# HDEncodeSource.search Tests
# ======================================================================

class TestSearch:
    """Tests for HDEncodeSource.search()."""

    def test_search_returns_results(self, source):
        """Search returns matching releases."""
        html = _build_search_results([
            ("Inception 2010 1080p BluRay-GROUP", "https://hdencode.org/inception/", False),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("Inception"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1

    def test_search_builds_correct_url(self, source):
        """Search URL should include encoded query."""
        html = _build_search_results([])
        calls = []
        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.search("The Dark Knight"))

        assert len(calls) == 1
        assert "?s=The+Dark+Knight" in calls[0]

    def test_search_mode_movies_filters_tv(self, source):
        """Search with mode='movies' filters out TV releases."""
        html = _build_search_results([
            ("Movie 2023 1080p-GROUP", "https://hdencode.org/movie/", False),
            ("Show S01E01 1080p-GROUP", "https://hdencode.org/show/", True),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("test", mode="movies"))

        # S01E01 release should be filtered out
        assert all(not r.is_tv for r in result.releases)

    def test_search_mode_tv_filters_movies(self, source):
        """Search with mode='tv' filters out movie releases."""
        html = _build_search_results([
            ("Movie 2023 1080p-GROUP", "https://hdencode.org/movie/", False),
            ("Show S01E01 1080p-GROUP", "https://hdencode.org/show/", True),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("test", mode="tv"))

        assert all(r.is_tv for r in result.releases)

    def test_search_html_failure(self, source):
        """Search returns error when HTML fetch fails."""
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

    def test_search_mode_all_no_filtering(self, source):
        """Search with mode='all' does not filter."""
        html = _build_search_results([
            ("Movie 2023 1080p-GROUP", "https://hdencode.org/movie/", False),
            ("Show S01E01 1080p-GROUP", "https://hdencode.org/show/", True),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.search("test", mode="all"))

        assert len(result.releases) == 2


# ======================================================================
# HDEncodeSource.fetch_release_details Tests
# ======================================================================

class TestFetchReleaseDetails:
    """Tests for HDEncodeSource.fetch_release_details()."""

    def test_fetch_details_basic(self, source):
        """Successfully fetch details."""
        html = _build_detail_page(
            "Inception 2010 1080p BluRay REMUX-FGT",
            content_html="<p>Size: 30 GB</p>",
            imdb_url="https://www.imdb.com/title/tt1375666/"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/inception/"))

        assert result is not None
        assert isinstance(result, ParsedRelease)

    def test_fetch_details_with_screenshots(self, source):
        """Screenshots should be extracted from content."""
        html = _build_detail_page(
            "Movie 2023 1080p-GROUP",
            screenshots=[
                "https://img.example.com/screenshot1.jpg",
                "https://img.example.com/screenshot2.jpg",
            ]
        )

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))

        assert result is not None
        assert len(result.screenshots) == 2

    def test_fetch_details_with_description(self, source):
        """Description should be extracted."""
        html = _build_detail_page(
            "Movie 2023 1080p-GROUP",
            description="A great movie about something."
        )

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))

        assert result is not None
        assert "great movie" in result.description

    def test_fetch_details_imdb_from_link(self, source):
        """IMDb ID extracted from link in content."""
        html = """
        <html><body>
        <h1 class="entry-title">Movie 2023 1080p-GROUP</h1>
        <div class="entry-content">
            <a href="https://www.imdb.com/title/tt1234567/">IMDb link</a>
        </div>
        </body></html>
        """

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))

        assert result is not None
        assert result.imdb_id == "tt1234567"

    def test_fetch_details_returns_none_on_html_failure(self, source):
        """Return None when HTML fetch fails."""
        with patch.object(source, '_fetch_html', return_value=None):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))
        assert result is None

    def test_fetch_details_returns_none_on_exception(self, source):
        """Return None on exception."""
        with patch.object(source, '_fetch_html', side_effect=RuntimeError("fail")):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))
        assert result is None

    def test_fetch_details_no_title_element(self, source):
        """Handle page with no title element."""
        html = "<html><body><div class='entry-content'>No title</div></body></html>"
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))
        assert result is None

    def test_fetch_details_no_content_section(self, source):
        """Handle page with no content section."""
        html = "<html><body><h1>Movie 2023 1080p-GROUP</h1></body></html>"
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))
        # parse_release still works, but no extra details
        assert result is not None

    def test_fetch_details_description_truncated(self, source):
        """Long descriptions should be truncated to 500 chars."""
        long_desc = "A" * 1000
        html = _build_detail_page(
            "Movie 2023 1080p-GROUP",
            description=long_desc
        )

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))

        assert result is not None
        assert len(result.description) <= 500

    def test_fetch_details_screenshots_limited_to_5(self, source):
        """Screenshots should be limited to 5."""
        screenshots = [f"https://img.example.com/screenshot{i}.jpg" for i in range(10)]
        html = _build_detail_page(
            "Movie 2023 1080p-GROUP",
            screenshots=screenshots
        )

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://hdencode.org/movie/"))

        assert result is not None
        assert len(result.screenshots) <= 5


# ======================================================================
# HDEncodeSource.fetch_download_links Tests
# ======================================================================

class TestFetchDownloadLinks:
    """Tests for HDEncodeSource.fetch_download_links()."""

    async def test_returns_empty_when_selenium_not_available(self, source):
        """Return empty list when selenium is not importable."""
        release = ParsedRelease(
            title="Movie 2023", url="https://hdencode.org/movie/", source="hdencode"
        )

        # Simulate selenium not being installed by blocking it in sys.modules
        with patch.dict('sys.modules', {'selenium': None, 'selenium.webdriver': None}):
            links = await source.fetch_download_links(release)

        assert links == []

    def test_returns_empty_on_webdriver_creation_failure(self, source):
        """Return empty list when WebDriver creation fails."""
        release = ParsedRelease(
            title="Movie 2023", url="https://hdencode.org/movie/", source="hdencode"
        )

        mock_selenium = MagicMock()
        mock_webdriver = MagicMock()
        mock_webdriver.Chrome.side_effect = RuntimeError("No ChromeDriver")
        mock_webdriver.ChromeOptions.return_value = MagicMock()
        mock_selenium.webdriver = mock_webdriver

        with patch.dict('sys.modules', {
            'selenium': mock_selenium,
            'selenium.webdriver': mock_webdriver,
            'selenium.webdriver.common': MagicMock(),
            'selenium.webdriver.common.by': MagicMock(),
            'selenium.webdriver.support': MagicMock(),
            'selenium.webdriver.support.ui': MagicMock(),
            'selenium.webdriver.support.expected_conditions': MagicMock(),
            'selenium.common': MagicMock(),
            'selenium.common.exceptions': MagicMock(),
        }):
            links = asyncio.run(source.fetch_download_links(release))

        assert links == []


# ======================================================================
# HDEncodeSource properties Tests
# ======================================================================

class TestProperties:
    """Tests for HDEncodeSource properties."""

    def test_name_property(self, source):
        """name property returns 'hdencode'."""
        assert source.name == "hdencode"

    def test_config_property(self, source):
        """config property returns SourceConfig."""
        assert isinstance(source.config, SourceConfig)

    def test_base_url(self):
        """BASE_URL class attribute."""
        assert HDEncodeSource.BASE_URL == "https://hdencode.org"

    def test_url_patterns(self):
        """URL_PATTERNS should have expected keys."""
        patterns = HDEncodeSource.URL_PATTERNS
        assert 'movies' in patterns
        assert 'movies_1080p' in patterns
        assert 'movies_4k' in patterns
        assert 'tv' in patterns
        assert 'tv_1080p' in patterns
        assert 'tv_4k' in patterns
