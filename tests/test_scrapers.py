"""Tests for backend/scrapers.py — WebScrapers class.

Covers:
- _title_to_rt_slug() slug generation
- scrape_details() HTML parsing logic (no network calls)
"""

import re
import pytest
from unittest.mock import MagicMock, patch

import sys, os
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.scrapers import WebScrapers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for a requests/cloudscraper Response."""

    def __init__(self, html: str, status_code: int = 200):
        self.status_code = status_code
        self.content = html.encode("utf-8")
        self.text = html


def _build_detail_html(
    filename: str,
    rating: str = "7.5",
    size_label: str = "FileSize: 15.5 GB",
    resolution: str = "Resolution: 1920x1080",
    color_primaries: str = "",
    imdb_url: str = "https://www.imdb.com/title/tt1234567/",
    extra_filenames: list | None = None,
):
    """Build a minimal HDEncode-style detail page for scrape_details()."""
    lines = []
    lines.append(f"Filename.....: {filename}")
    if extra_filenames:
        for fn in extra_filenames:
            lines.append(f"Filename.....: {fn}")
    lines.append(f"Rating : {rating}")
    lines.append(size_label)
    lines.append(resolution)
    if color_primaries:
        lines.append(f"Color primaries: {color_primaries}")
    text_block = "\n".join(lines)

    imdb_tag = f'<a href="{imdb_url}">IMDb</a>' if imdb_url else ""

    return f"""
    <html><body>
    <div class="entry-content">
    <pre>{text_block}</pre>
    {imdb_tag}
    </div>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_app():
    """Provide a MockApp from conftest (imported indirectly via conftest)."""
    from tests.conftest import MockApp
    return MockApp()


@pytest.fixture
def scraper(mock_app):
    """WebScrapers instance backed by MockApp."""
    return WebScrapers(mock_app)


# ===================================================================
# _title_to_rt_slug tests
# ===================================================================

class TestTitleToRtSlug:

    def test_simple_title(self, scraper):
        assert scraper._title_to_rt_slug("The Walking Dead") == "the_walking_dead"

    def test_special_chars_and_hyphens(self, scraper):
        result = scraper._title_to_rt_slug("Spider-Man: No Way Home")
        assert result == "spider_man_no_way_home"

    def test_empty_string(self, scraper):
        assert scraper._title_to_rt_slug("") == ""

    def test_single_word(self, scraper):
        assert scraper._title_to_rt_slug("Inception") == "inception"

    def test_multiple_spaces_collapsed(self, scraper):
        result = scraper._title_to_rt_slug("The   Big   Lebowski")
        assert result == "the_big_lebowski"

    def test_leading_trailing_stripped(self, scraper):
        result = scraper._title_to_rt_slug("  Dune  ")
        assert result == "dune"

    def test_ampersand_removed(self, scraper):
        result = scraper._title_to_rt_slug("Fast & Furious")
        assert result == "fast_furious"

    def test_apostrophe_removed(self, scraper):
        result = scraper._title_to_rt_slug("Schindler's List")
        # apostrophe is non-word special char, removed by regex
        assert result == "schindlers_list"

    def test_parentheses_removed(self, scraper):
        result = scraper._title_to_rt_slug("Alien (Director's Cut)")
        assert result == "alien_directors_cut"

    def test_numbers_preserved(self, scraper):
        result = scraper._title_to_rt_slug("2001: A Space Odyssey")
        assert result == "2001_a_space_odyssey"


# ===================================================================
# scrape_details – Movie parsing
# ===================================================================

class TestScrapeDetailsMovies:

    def _call(self, scraper, html, url="https://example.com/detail"):
        """Run scrape_details with a fake response, no network."""
        fake_resp = _FakeResponse(html)
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = fake_resp
        return scraper.scrape_details(url, headers={}, scraper=fake_scraper)

    def test_movie_title_and_year(self, scraper):
        html = _build_detail_html("Avatar.2009.1080p.BluRay.x264-GROUP.mkv")
        result = self._call(scraper, html)
        assert result is not None
        assert result["display_title"] == "Avatar"
        assert result["year"] == 2009

    def test_movie_resolution_1080p(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.BluRay.mkv")
        result = self._call(scraper, html)
        assert result["res"] == "1080p"

    def test_movie_resolution_4k_from_filename(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.UHD.BluRay.mkv")
        result = self._call(scraper, html)
        assert result["res"] == "4K"

    def test_movie_resolution_720p(self, scraper):
        html = _build_detail_html("Movie.2020.720p.BluRay.mkv")
        result = self._call(scraper, html)
        assert result["res"] == "720p"

    def test_imdb_id_extraction(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv",
            imdb_url="https://www.imdb.com/title/tt9876543/",
        )
        result = self._call(scraper, html)
        assert result["imdb_id"] == "tt9876543"
        assert "imdb.com" in result["imdb_link"]

    def test_no_imdb_link(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv", imdb_url="")
        result = self._call(scraper, html)
        assert result["imdb_id"] is None
        assert result["imdb_link"] is None

    def test_size_parsing(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv",
            size_label="FileSize: 15.5 GB",
        )
        result = self._call(scraper, html)
        assert "15.5" in result["size"]

    def test_hdr_detection_from_color_primaries(self, scraper):
        html = _build_detail_html(
            "Movie.2020.2160p.mkv",
            color_primaries="BT.2020",
        )
        result = self._call(scraper, html)
        assert result["hdr"] == "HDR"

    def test_dolby_vision_from_filename(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.DV.HDR.mkv")
        result = self._call(scraper, html)
        assert result["dovi"] is True

    def test_sdr_default(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv")
        result = self._call(scraper, html)
        assert result["hdr"] == "SDR"
        assert result["dovi"] is False

    def test_is_not_tv(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv")
        result = self._call(scraper, html)
        assert result["is_tv"] is False
        assert result["season"] is None

    def test_rating_extracted(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv", rating="8.1")
        result = self._call(scraper, html)
        assert result["rating"] == "8.1"

    def test_search_key_populated(self, scraper):
        html = _build_detail_html("The.Matrix.1999.1080p.mkv")
        result = self._call(scraper, html)
        assert result["search_key"] != ""
        # clean_string lowercases and removes special chars
        assert "matrix" in result["search_key"]

    def test_failed_response_returns_none(self, scraper):
        fake_resp = _FakeResponse("<html></html>", status_code=404)
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = fake_resp
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is None

    def test_no_filename_returns_none(self, scraper):
        html = "<html><body><div class='entry-content'>No filename here</div></body></html>"
        fake_resp = _FakeResponse(html)
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = fake_resp
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is None


# ===================================================================
# scrape_details – TV show parsing
# ===================================================================

class TestScrapeDetailsTVShows:

    def _call(self, scraper, html, url="https://example.com/detail"):
        fake_resp = _FakeResponse(html)
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = fake_resp
        return scraper.scrape_details(url, headers={}, scraper=fake_scraper)

    def test_tv_single_episode(self, scraper):
        html = _build_detail_html("Breaking.Bad.S01E01.1080p.BluRay.mkv")
        result = self._call(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 1
        assert result["episode_number"] == 1

    def test_tv_show_title_extracted(self, scraper):
        html = _build_detail_html("The.Walking.Dead.S03E05.1080p.mkv")
        result = self._call(scraper, html)
        assert result["is_tv"] is True
        assert "Walking Dead" in result["display_title"]

    def test_tv_season_pack_detection(self, scraper):
        """S01 without episode number => season pack."""
        html = _build_detail_html("Breaking.Bad.S01.1080p.BluRay.mkv")
        result = self._call(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 1
        # Season-only pattern: episode_number is None
        assert result["episode_number"] is None

    def test_tv_season_from_multi_unique_eps(self, scraper):
        """Multiple unique episode filenames => season pack override."""
        html = _build_detail_html(
            "Show.S01E01.1080p.mkv",
            extra_filenames=[
                "Show.S01E02.1080p.mkv",
                "Show.S01E03.1080p.mkv",
            ],
        )
        result = self._call(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 1
        # With >1 unique episodes, episode_number should be overridden to None
        assert result["episode_number"] is None
        assert result["episodes"] == 3

    def test_tv_year_is_zero(self, scraper):
        """TV shows use year=0 (not extracted from filename for matching)."""
        html = _build_detail_html("Show.S02E10.1080p.mkv")
        result = self._call(scraper, html)
        assert result["year"] == 0

    def test_tv_resolution_4k(self, scraper):
        html = _build_detail_html("Show.S01E01.2160p.mkv")
        result = self._call(scraper, html)
        assert result["res"] == "4K"
