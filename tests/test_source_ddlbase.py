"""Comprehensive tests for backend/sources/ddlbase.py module.

Covers:
- DDLBaseSource.get_config: returns correct SourceConfig
- DDLBaseSource.parse_release: parsing logic for various title formats
- DDLBaseSource._extract_display_title: title cleaning
- DDLBaseSource._extract_release_group: group extraction
- DDLBaseSource._parse_article: article HTML parsing
- DDLBaseSource._has_next_page: pagination detection
- DDLBaseSource.fetch_page: async page fetching with mocked HTTP
- DDLBaseSource.fetch_release_details: detail page fetching
- DDLBaseSource.fetch_download_links: link extraction from pages
- Edge cases: empty titles, missing elements, non-dict raw_data
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

from backend.sources.ddlbase import DDLBaseSource
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
    """Provide a DDLBaseSource instance with mocked scraper."""
    with patch.object(DDLBaseSource, '_get_scraper', return_value=MagicMock()):
        src = DDLBaseSource()
    return src


# ---------------------------------------------------------------------------
# Sample HTML builders
# ---------------------------------------------------------------------------

def _build_listing_page(articles, has_next=False):
    """Build a minimal DDLBase listing page HTML."""
    article_html = ""
    for title, url in articles:
        article_html += f"""
        <article class="type-post hentry">
            <h2><a href="{url}" rel="bookmark">{title}</a></h2>
            <div class="entry-content">
                <p>Some content here. FileSize: 45.2 GB</p>
            </div>
        </article>
        """
    pagination = ""
    if has_next:
        pagination = '<a class="next" href="/cat/movie-remux-1080p/page/2/">Next</a>'

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


def _build_detail_page(title, content_html="", imdb_id=None):
    """Build a minimal DDLBase detail page."""
    imdb_link = ""
    if imdb_id:
        imdb_link = f'<a href="https://www.imdb.com/title/{imdb_id}/">IMDb</a>'
    return f"""
    <html><body>
    <h1 class="entry-title">{title}</h1>
    <div class="entry-content">
        {content_html}
        {imdb_link}
    </div>
    </body></html>
    """


def _build_download_page(title, mirror_text="", extra_links=""):
    """Build a page with download links."""
    return f"""
    <html><body>
    <h1 class="entry-title">{title}</h1>
    <div class="entry-content">
        <p>{mirror_text}</p>
        {extra_links}
    </div>
    </body></html>
    """


# ======================================================================
# DDLBaseSource.get_config Tests
# ======================================================================

class TestGetConfig:
    """Tests for DDLBaseSource.get_config()."""

    def test_returns_source_config(self):
        """get_config should return a SourceConfig instance."""
        config = DDLBaseSource.get_config()
        assert isinstance(config, SourceConfig)

    def test_name(self):
        """Config name should be 'ddlbase'."""
        config = DDLBaseSource.get_config()
        assert config.name == "ddlbase"

    def test_display_name(self):
        """Config display_name should be 'DDLBase'."""
        config = DDLBaseSource.get_config()
        assert config.display_name == "DDLBase"

    def test_base_url(self):
        """Config base_url should be the DDLBase URL."""
        config = DDLBaseSource.get_config()
        assert config.base_url == "https://ddlbase.com"

    def test_capabilities_include_movies(self):
        """Capabilities should include MOVIES."""
        config = DDLBaseSource.get_config()
        assert SourceCapability.MOVIES in config.capabilities

    def test_capabilities_include_pagination(self):
        """Capabilities should include PAGINATION."""
        config = DDLBaseSource.get_config()
        assert SourceCapability.PAGINATION in config.capabilities

    def test_capabilities_include_direct_links(self):
        """Capabilities should include DIRECT_LINKS."""
        config = DDLBaseSource.get_config()
        assert SourceCapability.DIRECT_LINKS in config.capabilities

    def test_capabilities_include_cloudflare_bypass(self):
        """Capabilities should include CLOUDFLARE_BYPASS."""
        config = DDLBaseSource.get_config()
        assert SourceCapability.CLOUDFLARE_BYPASS in config.capabilities

    def test_capabilities_exclude_tv_shows(self):
        """Capabilities should NOT include TV_SHOWS (movies only)."""
        config = DDLBaseSource.get_config()
        assert SourceCapability.TV_SHOWS not in config.capabilities

    def test_enabled_by_default(self):
        """DDLBase should be enabled by default."""
        config = DDLBaseSource.get_config()
        assert config.enabled is True

    def test_requires_cloudflare_bypass(self):
        """DDLBase should require cloudflare bypass."""
        config = DDLBaseSource.get_config()
        assert config.requires_cloudflare_bypass is True

    def test_priority(self):
        """DDLBase should have priority 90."""
        config = DDLBaseSource.get_config()
        assert config.priority == 90

    def test_timeout(self):
        """DDLBase should have timeout 30."""
        config = DDLBaseSource.get_config()
        assert config.timeout == 30

    def test_rate_limit(self):
        """DDLBase should have rate_limit 2.0."""
        config = DDLBaseSource.get_config()
        assert config.rate_limit == 2.0


# ======================================================================
# DDLBaseSource.parse_release Tests
# ======================================================================

class TestParseRelease:
    """Tests for DDLBaseSource.parse_release()."""

    def test_returns_none_for_non_dict(self, source):
        """Non-dict raw_data should return None."""
        assert source.parse_release("not a dict") is None
        assert source.parse_release(123) is None
        assert source.parse_release(None) is None
        assert source.parse_release([]) is None

    def test_returns_none_for_empty_title(self, source):
        """Empty title should return None."""
        result = source.parse_release({'title': '', 'url': 'http://example.com'})
        assert result is None

    def test_basic_movie_release(self, source):
        """Parse a basic movie release title."""
        raw = {
            'title': 'The.Matrix.1999.1080p.BluRay.REMUX.AVC.DTS-HD MA.5.1-FGT',
            'url': 'https://ddlbase.com/the-matrix/',
            'default_resolution': '1080p',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert isinstance(result, ParsedRelease)
        assert result.source == "ddlbase"
        assert result.year == 1999
        assert result.resolution == "1080p"
        assert result.is_remux is True
        assert result.audio_codec == "DTS-HD MA"
        assert result.is_tv is False

    def test_4k_hdr_release(self, source):
        """Parse a 4K HDR release."""
        raw = {
            'title': 'Inception.2010.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.5.1-FGT',
            'url': 'https://ddlbase.com/inception/',
            'default_resolution': '4K',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.year == 2010
        assert result.resolution == "4K"
        assert result.is_hdr is True
        assert result.is_remux is True
        assert result.codec == "x265"

    def test_dolby_vision_release(self, source):
        """Parse a Dolby Vision release (space-separated Dolby Vision)."""
        raw = {
            'title': 'Dune 2021 2160p UHD BluRay REMUX Dolby Vision HEVC Atmos-FGT',
            'url': 'https://ddlbase.com/dune/',
            'default_resolution': '4K',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_hdr is True
        assert result.is_dovi is True
        assert result.audio_codec == "Atmos"

    def test_dolby_vision_dot_separated_detected(self, source):
        """Dolby.Vision with dot separator should be detected as Dolby Vision."""
        raw = {
            'title': 'Dune.2021.2160p.UHD.BluRay.REMUX.Dolby.Vision.HEVC.Atmos-FGT',
            'url': 'https://ddlbase.com/dune/',
            'default_resolution': '4K',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_hdr is True
        assert result.is_dovi is True

    def test_web_dl_release(self, source):
        """Parse a WEB-DL release."""
        raw = {
            'title': 'Movie.2023.1080p.WEB-DL.DD5.1.H.264-GROUP',
            'url': 'https://ddlbase.com/movie/',
            'default_resolution': '1080p',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.is_web is True
        assert result.is_remux is False

    def test_default_resolution_used_when_not_in_title(self, source):
        """When no resolution in title, default_resolution should be used."""
        raw = {
            'title': 'Some.Movie.2023.BluRay.REMUX-GROUP',
            'url': 'https://ddlbase.com/some-movie/',
            'default_resolution': '4K',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.resolution == "4K"

    def test_size_extraction_from_article_html(self, source):
        """Size should be extracted from article_html when not in title."""
        raw = {
            'title': 'Movie.2023.1080p.BluRay.REMUX-GROUP',
            'url': 'https://ddlbase.com/movie/',
            'default_resolution': '1080p',
            'article_html': '<div>FileSize: 45.2 GB</div>'
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.size == "45.2 GB"
        assert result.size_bytes > 0

    def test_imdb_extraction_from_article_html(self, source):
        """IMDb ID should be extracted from article_html."""
        raw = {
            'title': 'Movie.2023.1080p.BluRay.REMUX-GROUP',
            'url': 'https://ddlbase.com/movie/',
            'default_resolution': '1080p',
            'article_html': '<a href="https://imdb.com/title/tt1234567/">IMDb</a>'
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.imdb_id == "tt1234567"

    def test_no_imdb_when_no_article_html(self, source):
        """No IMDb ID when article_html is empty."""
        raw = {
            'title': 'Movie.2023.1080p.BluRay.REMUX-GROUP',
            'url': 'https://ddlbase.com/movie/',
            'default_resolution': '1080p',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.imdb_id is None

    def test_release_group_extraction(self, source):
        """Release group should be extracted from title."""
        raw = {
            'title': 'Movie.2023.1080p.BluRay.REMUX-FraMeSToR',
            'url': 'https://ddlbase.com/movie/',
            'default_resolution': '1080p',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.release_group == "FraMeSToR"

    def test_raw_data_stored(self, source):
        """Raw data dict should be stored in the release."""
        raw = {
            'title': 'Movie.2023.1080p-GROUP',
            'url': 'https://ddlbase.com/movie/',
            'default_resolution': '1080p',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.raw_data == raw

    def test_url_preserved(self, source):
        """URL should be preserved from raw_data."""
        raw = {
            'title': 'Movie.2023.1080p-GROUP',
            'url': 'https://ddlbase.com/specific-movie-page/',
            'default_resolution': '1080p',
            'article_html': ''
        }
        result = source.parse_release(raw)
        assert result is not None
        assert result.url == 'https://ddlbase.com/specific-movie-page/'


# ======================================================================
# DDLBaseSource._extract_display_title Tests
# ======================================================================

class TestExtractDisplayTitle:
    """Tests for DDLBaseSource._extract_display_title()."""

    def test_removes_quality_tags(self, source):
        """Quality tags like 1080p, BluRay, REMUX should be removed."""
        result = source._extract_display_title(
            "The Matrix 1999 1080p BluRay REMUX AVC DTS-HD MA 5.1-FGT", 1999
        )
        assert "1080p" not in result
        assert "BluRay" not in result
        assert "REMUX" not in result

    def test_preserves_title_and_year(self, source):
        """Title and year should be preserved."""
        result = source._extract_display_title(
            "Inception 2010 2160p UHD BluRay REMUX HDR HEVC-FGT", 2010
        )
        assert "Inception" in result
        assert "2010" in result

    def test_cleans_dots_and_underscores(self, source):
        """Dots and underscores should be replaced with spaces."""
        result = source._extract_display_title(
            "The.Dark.Knight.2008.1080p.BluRay.REMUX-GROUP", 2008
        )
        assert "." not in result
        assert "The" in result
        assert "Dark" in result

    def test_no_year_keeps_full_title(self, source):
        """When year=0, the full title (minus tags) should be kept."""
        result = source._extract_display_title(
            "Some Movie 1080p BluRay REMUX", 0
        )
        assert "Some Movie" in result

    def test_strips_whitespace(self, source):
        """Result should be stripped of leading/trailing whitespace."""
        result = source._extract_display_title(
            "  Movie  2023  1080p  ", 2023
        )
        assert result == result.strip()


# ======================================================================
# DDLBaseSource._extract_release_group Tests
# ======================================================================

class TestExtractReleaseGroup:
    """Tests for DDLBaseSource._extract_release_group()."""

    def test_extracts_group_at_end(self, source):
        """Release group at end of title after hyphen."""
        assert source._extract_release_group("Movie.2023.1080p-FraMeSToR") == "FraMeSToR"

    def test_extracts_group_before_brackets(self, source):
        """Release group before brackets."""
        assert source._extract_release_group("Movie.2023.1080p-FGT[rarbg]") == "FGT"

    def test_no_group_returns_empty(self, source):
        """No hyphen-group pattern returns empty string."""
        assert source._extract_release_group("Movie 2023") == ""

    def test_uses_last_hyphen_for_hyphenated_titles(self, source):
        """Hyphens in the title should not be mistaken for the release group."""
        assert source._extract_release_group("Spider-Man.No.Way.Home.2021.2160p-TEST") == "TEST"

    def test_group_before_trailing_comment(self, source):
        """Release group should still be found when extra text follows it."""
        assert source._extract_release_group("Movie.2023.1080p-TEST proper") == "TEST"


# ======================================================================
# DDLBaseSource._parse_article Tests
# ======================================================================

class TestParseArticle:
    """Tests for DDLBaseSource._parse_article()."""

    def test_parses_h2_a_link(self, source):
        """Parse article with h2 > a title link."""
        html = """
        <article class="type-post">
            <h2><a href="https://ddlbase.com/movie/">Movie 2023 1080p BluRay REMUX-GROUP</a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "1080p")
        assert result is not None
        assert result.resolution == "1080p"

    def test_parses_entry_title_link(self, source):
        """Parse article with .entry-title a link."""
        html = """
        <article class="type-post">
            <div class="entry-title"><a href="https://ddlbase.com/movie/">Movie 2023 4K UHD REMUX-GROUP</a></div>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "4K")
        assert result is not None

    def test_returns_none_when_no_title_link(self, source):
        """Return None when no title link found."""
        html = """
        <article class="type-post">
            <div class="content">No link here</div>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "1080p")
        assert result is None

    def test_returns_none_when_empty_title(self, source):
        """Return None when title text is empty."""
        html = """
        <article class="type-post">
            <h2><a href="https://ddlbase.com/movie/"></a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "1080p")
        assert result is None

    def test_returns_none_when_empty_href(self, source):
        """Return None when href is empty."""
        html = """
        <article class="type-post">
            <h2><a href="">Movie Title</a></h2>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "1080p")
        assert result is None

    def test_parses_bookmark_link(self, source):
        """Parse article with a[rel=bookmark] link."""
        html = """
        <article class="type-post">
            <a rel="bookmark" href="https://ddlbase.com/movie/">Movie 2023 1080p REMUX-GROUP</a>
        </article>
        """
        soup = BeautifulSoup(html, 'html.parser')
        article = soup.select_one('article')
        result = source._parse_article(article, "1080p")
        assert result is not None

    def test_exception_in_parsing_returns_none(self, source):
        """Exception during parsing should return None."""
        # Pass a non-BS4 element that will cause issues
        result = source._parse_article(None, "1080p")
        assert result is None


# ======================================================================
# DDLBaseSource._has_next_page Tests
# ======================================================================

class TestHasNextPage:
    """Tests for DDLBaseSource._has_next_page()."""

    def test_detects_next_link(self, source):
        """Detect next page via <a class='next'>."""
        html = '<div><a class="next" href="/page/2/">Next</a></div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_nav_next(self, source):
        """Detect next page via .nav-next a."""
        html = '<div class="nav-next"><a href="/page/2/">Next</a></div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_rel_next(self, source):
        """Detect next page via a[rel=next]."""
        html = '<div><a rel="next" href="/page/2/">Next</a></div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_detects_page_numbers_higher(self, source):
        """Detect next page via page numbers when higher number exists."""
        html = """
        <div class="pagination">
            <span class="page-numbers current">1</span>
            <a class="page-numbers" href="/page/2/">2</a>
            <a class="page-numbers" href="/page/3/">3</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is True

    def test_no_next_page(self, source):
        """Return False when no pagination found."""
        html = '<div>No pagination here</div>'
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False

    def test_last_page(self, source):
        """Return False when on the last page (no higher numbers)."""
        html = """
        <div class="pagination">
            <a class="page-numbers" href="/page/1/">1</a>
            <a class="page-numbers" href="/page/2/">2</a>
            <span class="page-numbers current">3</span>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False

    def test_non_numeric_current_page(self, source):
        """Handle non-numeric current page gracefully."""
        html = """
        <div class="pagination">
            <span class="page-numbers current">abc</span>
            <a class="page-numbers" href="/page/2/">2</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        # Should not raise, returns False due to ValueError
        assert source._has_next_page(soup) is False

    def test_non_numeric_page_links(self, source):
        """Handle non-numeric page links gracefully."""
        html = """
        <div class="pagination">
            <span class="page-numbers current">1</span>
            <a class="page-numbers" href="#">...</a>
        </div>
        """
        soup = BeautifulSoup(html, 'html.parser')
        assert source._has_next_page(soup) is False


# ======================================================================
# DDLBaseSource.fetch_page Tests
# ======================================================================

class TestFetchPage:
    """Tests for DDLBaseSource.fetch_page()."""

    def test_fetch_page_1080p(self, source):
        """Fetch page with 1080p resolution filter."""
        html = _build_listing_page([
            ("Movie.2023.1080p.BluRay.REMUX-GROUP", "https://ddlbase.com/movie/"),
        ], has_next=True)

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1
        assert result.has_next is True
        assert result.errors == []

    def test_fetch_page_4k(self, source):
        """Fetch page with 4K resolution filter."""
        html = _build_listing_page([
            ("Movie.2023.2160p.UHD.BluRay.REMUX-GROUP", "https://ddlbase.com/movie/"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="4K"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1

    def test_fetch_page_2160p_alias(self, source):
        """2160p resolution should be treated same as 4K."""
        html = _build_listing_page([
            ("Movie.2023.2160p.UHD.BluRay.REMUX-GROUP", "https://ddlbase.com/movie/"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="2160p"))

        assert isinstance(result, PageResult)
        assert len(result.releases) == 1

    def test_fetch_page_default_resolution_combines_both(self, source):
        """Default resolution (None) should combine 1080p and 4K results."""
        html = _build_listing_page([
            ("Movie.2023.1080p.BluRay.REMUX-GROUP", "https://ddlbase.com/movie/"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies"))

        assert isinstance(result, PageResult)
        # Both 1080p and 4K pages are fetched, each returns 1 release
        assert len(result.releases) == 2

    def test_fetch_page_pagination_url(self, source):
        """Page > 1 should use pagination URL."""
        html = _build_listing_page([])
        calls = []

        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=3, mode="movies", resolution="1080p"))

        assert len(calls) == 1
        assert "/page/3/" in calls[0]

    def test_fetch_page_1_no_pagination_suffix(self, source):
        """Page 1 should not use /page/1/ URL suffix."""
        html = _build_listing_page([])
        calls = []

        def mock_fetch(url):
            calls.append(url)
            return html

        with patch.object(source, '_fetch_html', side_effect=mock_fetch):
            asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        assert len(calls) == 1
        assert "/page/" not in calls[0]

    def test_fetch_page_html_none_returns_errors(self, source):
        """When HTML fetch returns None, result should have errors."""
        with patch.object(source, '_fetch_html', return_value=None):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        assert result.releases == []
        assert len(result.errors) > 0

    def test_fetch_page_exception_returns_errors(self, source):
        """When an exception occurs, result should have errors."""
        with patch.object(source, '_fetch_html', side_effect=RuntimeError("network error")):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        assert result.releases == []
        assert len(result.errors) > 0

    def test_fetch_page_article_parse_error_skips(self, source):
        """Articles that fail to parse should be skipped, not crash."""
        html = """
        <html><body>
        <article class="type-post">
            <h2><a href="https://ddlbase.com/good/">Good Movie 2023 1080p REMUX-GROUP</a></h2>
        </article>
        <article class="type-post">
            <!-- Missing link, will return None -->
            <div>No link here</div>
        </article>
        </body></html>
        """
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        # Only the good article should parse
        assert len(result.releases) == 1

    def test_fetch_page_multiple_articles(self, source):
        """Multiple articles should all be parsed."""
        html = _build_listing_page([
            ("Movie.One.2023.1080p.BluRay.REMUX-GROUP", "https://ddlbase.com/one/"),
            ("Movie.Two.2022.1080p.BluRay.REMUX-TEAM", "https://ddlbase.com/two/"),
            ("Movie.Three.2021.1080p.BluRay.REMUX-FGT", "https://ddlbase.com/three/"),
        ])

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_page(page=1, mode="movies", resolution="1080p"))

        assert len(result.releases) == 3


# ======================================================================
# DDLBaseSource.fetch_release_details Tests
# ======================================================================

class TestFetchReleaseDetails:
    """Tests for DDLBaseSource.fetch_release_details()."""

    def test_fetch_details_success(self, source):
        """Successfully fetch and parse release details."""
        html = _build_detail_page(
            "Inception.2010.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-FGT",
            content_html="<p>FileSize: 30.5 GB</p>",
            imdb_id="tt1375666"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://ddlbase.com/inception/"))

        assert result is not None
        assert isinstance(result, ParsedRelease)
        assert result.year == 2010

    def test_fetch_details_returns_none_on_html_failure(self, source):
        """Return None when HTML fetch fails."""
        with patch.object(source, '_fetch_html', return_value=None):
            result = asyncio.run(source.fetch_release_details("https://ddlbase.com/movie/"))

        assert result is None

    def test_fetch_details_returns_none_on_exception(self, source):
        """Return None when exception occurs."""
        with patch.object(source, '_fetch_html', side_effect=RuntimeError("fail")):
            result = asyncio.run(source.fetch_release_details("https://ddlbase.com/movie/"))

        assert result is None

    def test_fetch_details_no_title_element(self, source):
        """Handle page with no title element."""
        html = "<html><body><div class='entry-content'>Content</div></body></html>"
        with patch.object(source, '_fetch_html', return_value=html):
            result = asyncio.run(source.fetch_release_details("https://ddlbase.com/movie/"))

        # parse_release will receive empty title, returning None
        assert result is None


# ======================================================================
# DDLBaseSource.fetch_download_links Tests
# ======================================================================

class TestFetchDownloadLinks:
    """Tests for DDLBaseSource.fetch_download_links()."""

    def test_extract_cuty_links_from_mirror1(self, source):
        """Extract cuty.io links from Mirror 1 section."""
        html = _build_download_page(
            "Movie 2023",
            mirror_text=(
                "Mirror 1: "
                "https://cuty.io/abc123 "
                "https://cuty.io/def456 "
                "Mirror 2: "
                "https://cuty.io/other789"
            )
        )

        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release))

        assert "https://cuty.io/abc123" in links
        assert "https://cuty.io/def456" in links

    def test_extract_cuty_links_fallback_when_no_mirror1(self, source):
        """When no Mirror 1 section, fallback to all cuty.io links."""
        html = _build_download_page(
            "Movie 2023",
            extra_links='<a href="https://cuty.io/xyz789">Link</a>'
        )

        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release))

        assert "https://cuty.io/xyz789" in links

    def test_extract_1fichier_links(self, source):
        """Extract direct 1fichier.com links."""
        html = _build_download_page(
            "Movie 2023",
            extra_links='<a href="https://1fichier.com/?abc123&def=456">Download</a>'
        )

        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release))

        assert any("1fichier.com" in link for link in links)

    def test_deduplicates_links(self, source):
        """Duplicate links should be removed."""
        html = _build_download_page(
            "Movie 2023",
            extra_links=(
                '<a href="https://cuty.io/abc123">Link 1</a>'
                '<a href="https://cuty.io/abc123">Link 1 dup</a>'
                '<a href="https://cuty.io/def456">Link 2</a>'
            )
        )

        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release))

        # Count unique links - no duplicates
        assert len(links) == len(set(links))

    def test_returns_empty_on_html_failure(self, source):
        """Return empty list when HTML fetch fails."""
        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', return_value=None):
            links = asyncio.run(source.fetch_download_links(release))

        assert links == []

    def test_returns_empty_on_no_content(self, source):
        """Return empty list when no entry-content found."""
        html = "<html><body><div>No content section</div></body></html>"
        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', return_value=html):
            links = asyncio.run(source.fetch_download_links(release))

        assert links == []

    def test_returns_empty_on_exception(self, source):
        """Return empty list when exception occurs."""
        release = ParsedRelease(
            title="Movie 2023",
            url="https://ddlbase.com/movie/",
            source="ddlbase"
        )

        with patch.object(source, '_fetch_html', side_effect=RuntimeError("fail")):
            links = asyncio.run(source.fetch_download_links(release))

        assert links == []


# ======================================================================
# DDLBaseSource._fetch_html Tests
# ======================================================================

class TestFetchHtml:
    """Tests for DDLBaseSource._fetch_html()."""

    def test_successful_fetch(self, source):
        """Return HTML text on successful fetch."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html>content</html>"

        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://ddlbase.com/test/")

        assert result == "<html>content</html>"

    def test_non_200_returns_none(self, source):
        """Return None on non-200 status code."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_scraper = MagicMock()
        mock_scraper.get.return_value = mock_response

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://ddlbase.com/missing/")

        assert result is None

    def test_exception_returns_none(self, source):
        """Return None on exception."""
        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = ConnectionError("timeout")

        with patch.object(source, '_get_scraper', return_value=mock_scraper):
            result = source._fetch_html("https://ddlbase.com/fail/")

        assert result is None


# ======================================================================
# DDLBaseSource properties and name Tests
# ======================================================================

class TestProperties:
    """Tests for DDLBaseSource properties."""

    def test_name_property(self, source):
        """name property should return 'ddlbase'."""
        assert source.name == "ddlbase"

    def test_config_property(self, source):
        """config property should return SourceConfig."""
        assert isinstance(source.config, SourceConfig)
        assert source.config.name == "ddlbase"

    def test_base_url_class_attribute(self):
        """BASE_URL class attribute should be correct."""
        assert DDLBaseSource.BASE_URL == "https://ddlbase.com"

    def test_url_patterns(self):
        """URL_PATTERNS should have correct paths."""
        assert 'movies_1080p' in DDLBaseSource.URL_PATTERNS
        assert 'movies_4k' in DDLBaseSource.URL_PATTERNS
        assert DDLBaseSource.URL_PATTERNS['movies_1080p'] == '/cat/movie-remux-1080p/'
        assert DDLBaseSource.URL_PATTERNS['movies_4k'] == '/cat/movie-remux-2160p/'
