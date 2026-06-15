"""Extended tests for backend/scrapers.py — WebScrapers class.

Covers pure parsing and string methods without network calls:
- _title_to_rt_slug: various URL slug patterns
- scrape_details: title parsing with year, season, episodes, resolution, codec info
- scrape_details: size extraction from HTML ("15.5 GB", "500 MB", "1.2 TB")
- scrape_details: resolution extraction (2160p -> "4K", "1080p", "720p")
- scrape_details: HDR detection (HDR10, Dolby Vision)
- scrape_details: IMDb ID extraction
- scrape_imdb_data: mocked HTTP response parsing
- scrape_rt_score: mocked HTTP response parsing
"""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

import sys
import os

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

    def json(self):
        return json.loads(self.text)


def _build_detail_html(
    filename,
    rating="7.5",
    size_label="FileSize: 15.5 GB",
    resolution="Resolution: 1920x1080",
    color_primaries="",
    imdb_url="https://www.imdb.com/title/tt1234567/",
    extra_filenames=None,
    extra_text="",
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
    if extra_text:
        lines.append(extra_text)
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


class MockApp:
    """Minimal mock of the parent app object for WebScrapers."""

    def __init__(self, config=None):
        self.config = config or {}
        self.download_history = set()
        self.tmdb_cache = {}
        self._logs = []

    def clean_string(self, s):
        import re
        if not s:
            return ""
        normalized = s.lower().strip()
        normalized = re.sub(r'\((\d{4})\)', '', normalized)
        normalized = re.sub(r'\b(19|20)\d{2}\b', '', normalized)
        normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def parse_size(self, s):
        import re
        try:
            if not s or not isinstance(s, str) or s == "?":
                return 0.0
            s_clean = str(s).upper().replace(' ', '')
            if 'TB' in s_clean or 'TIB' in s_clean:
                return float(re.sub(r'[A-Z]+', '', s_clean)) * 1024
            elif 'GB' in s_clean or 'GIB' in s_clean:
                return float(re.sub(r'[A-Z]+', '', s_clean))
            elif 'MB' in s_clean or 'MIB' in s_clean:
                return float(re.sub(r'[A-Z]+', '', s_clean)) / 1024
            return float(re.sub(r'[A-Z]+', '', s_clean))
        except (ValueError, TypeError):
            return 0.0

    def safe_log(self, msg):
        self._logs.append(msg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_app():
    return MockApp()


@pytest.fixture
def scraper(mock_app):
    return WebScrapers(mock_app)


def _scrape(scraper, html, url="https://example.com/detail"):
    """Run scrape_details with a fake response, no network calls."""
    fake_resp = _FakeResponse(html)
    fake_scraper = MagicMock()
    fake_scraper.get.return_value = fake_resp
    return scraper.scrape_details(url, headers={}, scraper=fake_scraper)


# ===================================================================
# _title_to_rt_slug — extended tests
# ===================================================================

class TestTitleToRtSlugExtended:

    def test_simple_title(self, scraper):
        assert scraper._title_to_rt_slug("The Walking Dead") == "the_walking_dead"

    def test_title_with_colon(self, scraper):
        assert scraper._title_to_rt_slug("Avengers: Endgame") == "avengers_endgame"

    def test_title_with_hyphens(self, scraper):
        result = scraper._title_to_rt_slug("Spider-Man")
        assert result == "spider_man"

    def test_title_with_multiple_hyphens(self, scraper):
        result = scraper._title_to_rt_slug("Ant-Man and the Wasp: Quantumania")
        assert result == "ant_man_and_the_wasp_quantumania"

    def test_title_with_ampersand(self, scraper):
        result = scraper._title_to_rt_slug("Fast & Furious")
        assert result == "fast_furious"

    def test_title_with_apostrophe(self, scraper):
        result = scraper._title_to_rt_slug("Ocean's Eleven")
        assert result == "oceans_eleven"

    def test_title_with_dots(self, scraper):
        result = scraper._title_to_rt_slug("Dr. Strange")
        assert result == "dr_strange"

    def test_empty_string(self, scraper):
        assert scraper._title_to_rt_slug("") == ""

    def test_single_word(self, scraper):
        assert scraper._title_to_rt_slug("Gladiator") == "gladiator"

    def test_numbers_preserved(self, scraper):
        assert scraper._title_to_rt_slug("2001: A Space Odyssey") == "2001_a_space_odyssey"

    def test_trailing_leading_spaces_stripped(self, scraper):
        assert scraper._title_to_rt_slug("  Dune  ") == "dune"

    def test_multiple_spaces_collapsed(self, scraper):
        assert scraper._title_to_rt_slug("The   Big   Lebowski") == "the_big_lebowski"

    def test_parentheses_content_removed(self, scraper):
        result = scraper._title_to_rt_slug("Blade Runner (Final Cut)")
        assert result == "blade_runner_final_cut"

    def test_exclamation_and_question_marks_removed(self, scraper):
        result = scraper._title_to_rt_slug("Who Framed Roger Rabbit?!")
        assert result == "who_framed_roger_rabbit"

    def test_unicode_word_chars_preserved(self, scraper):
        # Word chars (\w) include underscores; unicode letters depend on locale
        result = scraper._title_to_rt_slug("Amelie")
        assert result == "amelie"

    def test_all_special_chars(self, scraper):
        result = scraper._title_to_rt_slug("@#$%^*!")
        assert result == ""

    def test_mixed_separators(self, scraper):
        result = scraper._title_to_rt_slug("Run - Hide - Fight")
        assert result == "run_hide_fight"

    def test_title_with_number_sequel(self, scraper):
        result = scraper._title_to_rt_slug("Deadpool 2")
        assert result == "deadpool_2"

    def test_title_with_roman_numerals(self, scraper):
        result = scraper._title_to_rt_slug("Rocky III")
        assert result == "rocky_iii"


# ===================================================================
# scrape_details — title parsing with year
# ===================================================================

class TestScrapeDetailsTitleParsing:

    def test_standard_movie_title_year(self, scraper):
        html = _build_detail_html("Inception.2010.1080p.BluRay.x264.mkv")
        result = _scrape(scraper, html)
        assert result is not None
        assert result["display_title"] == "Inception"
        assert result["year"] == 2010

    def test_title_with_spaces_via_dots(self, scraper):
        html = _build_detail_html("The.Grand.Budapest.Hotel.2014.1080p.mkv")
        result = _scrape(scraper, html)
        assert result["display_title"] == "The Grand Budapest Hotel"
        assert result["year"] == 2014

    def test_title_with_parenthesized_year(self, scraper):
        html = _build_detail_html("Oppenheimer (2023) 1080p.mkv")
        result = _scrape(scraper, html)
        assert result is not None
        assert result["year"] == 2023

    def test_title_without_year(self, scraper):
        html = _build_detail_html("SomeWeirdFile.mkv")
        result = _scrape(scraper, html)
        assert result is not None
        assert result["year"] == 0

    def test_title_with_year_1990s(self, scraper):
        html = _build_detail_html("Pulp.Fiction.1994.720p.mkv")
        result = _scrape(scraper, html)
        assert result["display_title"] == "Pulp Fiction"
        assert result["year"] == 1994

    def test_title_with_dash_separator(self, scraper):
        html = _build_detail_html("No.Country.for.Old.Men.2007.1080p-GROUP.mkv")
        result = _scrape(scraper, html)
        assert result["display_title"] == "No Country for Old Men"
        assert result["year"] == 2007


# ===================================================================
# scrape_details — TV show parsing (season, episodes)
# ===================================================================

class TestScrapeDetailsTVParsing:

    def test_single_episode_pattern(self, scraper):
        html = _build_detail_html("Breaking.Bad.S01E01.1080p.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 1
        assert result["episode_number"] == 1

    def test_season_pack_pattern(self, scraper):
        html = _build_detail_html("The.Bear.S02.1080p.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 2
        assert result["episode_number"] is None

    def test_tv_show_title_extraction(self, scraper):
        html = _build_detail_html("The.Walking.Dead.S03E05.1080p.mkv")
        result = _scrape(scraper, html)
        assert "Walking Dead" in result["display_title"]
        assert result["season"] == 3
        assert result["episode_number"] == 5

    def test_tv_year_is_zero(self, scraper):
        html = _build_detail_html("Show.Name.S05E01.mkv")
        result = _scrape(scraper, html)
        assert result["year"] == 0

    def test_multi_episode_season_pack(self, scraper):
        html = _build_detail_html(
            "Show.S01E01.1080p.mkv",
            extra_filenames=[
                "Show.S01E02.1080p.mkv",
                "Show.S01E03.1080p.mkv",
                "Show.S01E04.1080p.mkv",
            ],
        )
        result = _scrape(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 1
        # Multiple unique episodes => season pack, episode_number overridden
        assert result["episode_number"] is None
        assert result["episodes"] == 4

    def test_duplicate_episode_files_not_overcounted(self, scraper):
        """If the same episode number appears in multiple filenames (mirrors),
        unique episode count should reflect actual unique episodes."""
        html = _build_detail_html(
            "Show.S01E01.1080p.mkv",
            extra_filenames=[
                "Show.S01E01.1080p.PROPER.mkv",  # same E01
                "Show.S01E02.1080p.mkv",
            ],
        )
        result = _scrape(scraper, html)
        assert result["is_tv"] is True
        # Only 2 unique episodes: E01 and E02
        assert result["episodes"] == 2

    def test_high_season_number(self, scraper):
        html = _build_detail_html("Simpsons.S35E10.1080p.mkv")
        result = _scrape(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 35
        assert result["episode_number"] == 10

    def test_tv_with_dash_separator(self, scraper):
        html = _build_detail_html("Show-Name-S02E05.1080p.mkv")
        result = _scrape(scraper, html)
        assert result["is_tv"] is True
        assert result["season"] == 2


# ===================================================================
# scrape_details — size extraction
# ===================================================================

class TestScrapeDetailsSize:

    def test_size_gb(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="FileSize: 15.5 GB"
        )
        result = _scrape(scraper, html)
        assert "15.5" in result["size"]
        assert "GB" in result["size"]

    def test_size_mb(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="FileSize: 500 MB"
        )
        result = _scrape(scraper, html)
        assert "500" in result["size"]
        assert "MB" in result["size"]

    def test_size_tb_not_matched_by_regex(self, scraper):
        """The size regex only handles GiB/GB/MiB/MB/KB, not TB.
        A size like '1.2 TB' will not be captured."""
        html = _build_detail_html(
            "Movie.2020.2160p.mkv", size_label="Total Size: 1.2 TB"
        )
        result = _scrape(scraper, html)
        # TB is not in the regex alternation, so size defaults to "?"
        assert result["size"] == "?"

    def test_size_large_gb_equivalent_of_tb(self, scraper):
        """When the page lists a TB-equivalent in GB, it should parse correctly."""
        html = _build_detail_html(
            "Movie.2020.2160p.mkv", size_label="Total Size: 1228.8 GB"
        )
        result = _scrape(scraper, html)
        assert "1228.8" in result["size"]

    def test_size_gib(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="Size: 20.3 GiB"
        )
        result = _scrape(scraper, html)
        assert "20.3" in result["size"]

    def test_size_mib(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="Size: 800 MiB"
        )
        result = _scrape(scraper, html)
        assert "800" in result["size"]

    def test_size_with_dots_label(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="FileSize.....: 25 GB"
        )
        result = _scrape(scraper, html)
        assert "25" in result["size"]

    def test_no_size_returns_question_mark(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="No size info here at all"
        )
        result = _scrape(scraper, html)
        assert result["size"] == "?"

    def test_largest_size_selected(self, scraper):
        """When multiple sizes appear, the largest should be selected."""
        html = _build_detail_html(
            "Movie.2020.1080p.mkv",
            size_label="FileSize: 5.5 GB\nTotal Size: 55 GB",
        )
        result = _scrape(scraper, html)
        assert "55" in result["size"]

    def test_size_decimal(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv", size_label="FileSize: 0.85 GB"
        )
        result = _scrape(scraper, html)
        assert "0.85" in result["size"]


# ===================================================================
# scrape_details — resolution extraction
# ===================================================================

class TestScrapeDetailsResolution:

    def test_resolution_4k_from_2160p_in_filename(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "4K"

    def test_resolution_4k_from_uhd_in_filename(self, scraper):
        html = _build_detail_html("Movie.2020.UHD.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "4K"

    def test_resolution_4k_from_4k_in_filename(self, scraper):
        html = _build_detail_html("Movie.2020.4K.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "4K"

    def test_resolution_1080p_from_filename(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "1080p"

    def test_resolution_720p_from_filename(self, scraper):
        html = _build_detail_html("Movie.2020.720p.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "720p"

    def test_resolution_from_metadata_field_3840(self, scraper):
        """When filename has no resolution hint, fall back to Resolution field."""
        html = _build_detail_html(
            "Movie.2020.BluRay.mkv",
            resolution="Resolution: 3840x2160",
        )
        result = _scrape(scraper, html)
        assert result["res"] == "4K"

    def test_resolution_from_metadata_field_1920(self, scraper):
        html = _build_detail_html(
            "Movie.2020.BluRay.mkv",
            resolution="Resolution: 1920x1080",
        )
        result = _scrape(scraper, html)
        # Filename has no res hint, but Resolution field has 1080
        assert result["res"] == "1080p"

    def test_resolution_filename_overrides_metadata(self, scraper):
        """Filename resolution takes precedence over metadata field."""
        html = _build_detail_html(
            "Movie.2020.2160p.BluRay.mkv",
            resolution="Resolution: 1920x1080",
        )
        result = _scrape(scraper, html)
        assert result["res"] == "4K"

    def test_resolution_unknown_when_nothing_found(self, scraper):
        html = _build_detail_html(
            "Movie.2020.BluRay.mkv",
            resolution="No resolution data",
        )
        result = _scrape(scraper, html)
        assert result["res"] == "?"


# ===================================================================
# scrape_details — HDR / Dolby Vision detection
# ===================================================================

class TestScrapeDetailsHDR:

    def test_sdr_default(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.BluRay.mkv")
        result = _scrape(scraper, html)
        assert result["hdr"] == "SDR"
        assert result["dovi"] is False

    def test_hdr_from_color_primaries_bt2020(self, scraper):
        html = _build_detail_html(
            "Movie.2020.2160p.mkv",
            color_primaries="BT.2020",
        )
        result = _scrape(scraper, html)
        assert result["hdr"] == "HDR"

    def test_hdr_from_color_primaries_hdr_keyword(self, scraper):
        html = _build_detail_html(
            "Movie.2020.2160p.mkv",
            color_primaries="HDR10",
        )
        result = _scrape(scraper, html)
        assert result["hdr"] == "HDR"

    def test_dolby_vision_from_filename_dv(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.DV.HDR.mkv")
        result = _scrape(scraper, html)
        assert result["dovi"] is True

    def test_dolby_vision_from_filename_dovi(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.DoVi.mkv")
        result = _scrape(scraper, html)
        assert result["dovi"] is True

    def test_dolby_vision_from_filename_dolby_vision(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.DolbyVision.mkv")
        result = _scrape(scraper, html)
        assert result["dovi"] is True

    def test_dolby_vision_from_color_primaries(self, scraper):
        html = _build_detail_html(
            "Movie.2020.2160p.mkv",
            color_primaries="Dolby Vision / BT.2020",
        )
        result = _scrape(scraper, html)
        assert result["dovi"] is True

    def test_hdr_and_dv_both_detected(self, scraper):
        html = _build_detail_html(
            "Movie.2020.2160p.DV.mkv",
            color_primaries="BT.2020",
        )
        result = _scrape(scraper, html)
        assert result["hdr"] == "HDR"
        assert result["dovi"] is True

    def test_dv_in_color_primaries_only(self, scraper):
        html = _build_detail_html(
            "Movie.2020.2160p.mkv",
            color_primaries="dovi profile 8.1",
        )
        result = _scrape(scraper, html)
        assert result["dovi"] is True


# ===================================================================
# scrape_details — IMDb extraction
# ===================================================================

class TestScrapeDetailsIMDb:

    def test_imdb_id_extracted(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv",
            imdb_url="https://www.imdb.com/title/tt9876543/",
        )
        result = _scrape(scraper, html)
        assert result["imdb_id"] == "tt9876543"
        assert "imdb.com" in result["imdb_link"]

    def test_no_imdb_link(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv", imdb_url="")
        result = _scrape(scraper, html)
        assert result["imdb_id"] is None
        assert result["imdb_link"] is None

    def test_imdb_id_with_many_digits(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv",
            imdb_url="https://www.imdb.com/title/tt12345678/",
        )
        result = _scrape(scraper, html)
        assert result["imdb_id"] == "tt12345678"

    def test_imdb_link_without_trailing_slash(self, scraper):
        html = _build_detail_html(
            "Movie.2020.1080p.mkv",
            imdb_url="https://www.imdb.com/title/tt0000001",
        )
        result = _scrape(scraper, html)
        assert result["imdb_id"] == "tt0000001"


# ===================================================================
# scrape_details — edge cases
# ===================================================================

class TestScrapeDetailsEdgeCases:

    def test_failed_http_returns_none(self, scraper):
        fake_resp = _FakeResponse("<html></html>", status_code=404)
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = fake_resp
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is None

    def test_no_filename_in_page_returns_none(self, scraper):
        html = "<html><body><div class='entry-content'>No filename here</div></body></html>"
        result = _scrape(scraper, html)
        assert result is None

    def test_retry_on_transient_failure(self, scraper):
        """scrape_details retries up to 3 times on failure."""
        fail_resp = _FakeResponse("", status_code=500)
        ok_html = _build_detail_html("Movie.2020.1080p.mkv")
        ok_resp = _FakeResponse(ok_html, status_code=200)
        fake_scraper = MagicMock()
        fake_scraper.get.side_effect = [fail_resp, fail_resp, ok_resp]
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is not None
        assert result["display_title"] == "Movie"

    def test_rate_limited_429_retries(self, scraper):
        """429 Too Many Requests should trigger retry."""
        rate_limit_resp = _FakeResponse("", status_code=429)
        ok_html = _build_detail_html("Film.2021.4K.mkv")
        ok_resp = _FakeResponse(ok_html, status_code=200)
        fake_scraper = MagicMock()
        fake_scraper.get.side_effect = [rate_limit_resp, ok_resp]
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is not None

    def test_connection_error_retries(self, scraper):
        """Network exceptions should trigger retry."""
        ok_html = _build_detail_html("Movie.2020.1080p.mkv")
        ok_resp = _FakeResponse(ok_html, status_code=200)
        fake_scraper = MagicMock()
        fake_scraper.get.side_effect = [ConnectionError("timeout"), ok_resp]
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is not None

    def test_all_retries_fail_returns_none(self, scraper):
        """When all 3 retries fail, should return None."""
        fail_resp = _FakeResponse("", status_code=500)
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = fail_resp
        result = scraper.scrape_details("https://example.com", {}, scraper=fake_scraper)
        assert result is None

    def test_rating_extraction(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv", rating="8.9")
        result = _scrape(scraper, html)
        assert result["rating"] == "8.9"

    def test_search_key_populated(self, scraper):
        html = _build_detail_html("The.Matrix.1999.1080p.mkv")
        result = _scrape(scraper, html)
        assert result["search_key"] != ""
        assert "matrix" in result["search_key"]

    def test_scraper_creates_own_if_none_provided(self, scraper):
        """When no scraper arg is passed, scrape_details creates one internally."""
        with patch("backend.detail_scraper.cloudscraper") as mock_cs:
            fake_scraper = MagicMock()
            ok_html = _build_detail_html("Movie.2020.1080p.mkv")
            fake_scraper.get.return_value = _FakeResponse(ok_html)
            mock_cs.create_scraper.return_value = fake_scraper
            result = scraper.scrape_details("https://example.com", {}, scraper=None)
            mock_cs.create_scraper.assert_called_once()
            assert result is not None

    def test_url_field_stored(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.mkv")
        result = _scrape(scraper, html, url="https://hdencode.org/movie-detail/")
        assert result["url"] == "https://hdencode.org/movie-detail/"


# ===================================================================
# scrape_details — codec / filename edge patterns
# ===================================================================

class TestScrapeDetailsCodecPatterns:

    def test_x264_in_filename(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.BluRay.x264-GROUP.mkv")
        result = _scrape(scraper, html)
        assert result is not None
        assert result["res"] == "1080p"

    def test_x265_hevc_in_filename(self, scraper):
        html = _build_detail_html("Movie.2020.2160p.BluRay.x265.HEVC-GROUP.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "4K"

    def test_remux_in_filename(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.Remux.AVC-GROUP.mkv")
        result = _scrape(scraper, html)
        assert result["res"] == "1080p"

    def test_hdr10plus_in_filename(self, scraper):
        """HDR10+ detection via color primaries (filename DV only checks DV words)."""
        html = _build_detail_html(
            "Movie.2020.2160p.HDR10Plus.mkv",
            color_primaries="BT.2020 / HDR10+",
        )
        result = _scrape(scraper, html)
        assert result["hdr"] == "HDR"

    def test_atmos_in_filename_does_not_affect_hdr(self, scraper):
        html = _build_detail_html("Movie.2020.1080p.Atmos.mkv")
        result = _scrape(scraper, html)
        assert result["hdr"] == "SDR"

    def test_mojibake_title_repair(self, scraper):
        """cp437 mojibake repair is attempted on title."""
        # This test verifies the try/except path; actual mojibake may or may
        # not decode depending on the specific bytes.
        html = _build_detail_html("Normal.Title.2020.1080p.mkv")
        result = _scrape(scraper, html)
        assert result is not None
        assert isinstance(result["display_title"], str)


# ===================================================================
# scrape_imdb_data — mocked HTTP
# ===================================================================

class TestScrapeImdbData:

    def test_valid_imdb_page_returns_rating_votes(self, scraper):
        ld_json = json.dumps({
            "aggregateRating": {
                "ratingValue": 8.7,
                "ratingCount": 1500000,
            }
        })
        html = f"""
        <html><head>
        <script type="application/ld+json">{ld_json}</script>
        </head><body></body></html>
        """
        with patch("backend.imdb_scraper.requests") as mock_requests:
            mock_requests.get.return_value = _FakeResponse(html, 200)
            result = scraper.scrape_imdb_data("tt0133093")
        assert result is not None
        assert result["rating"] == 8.7
        assert result["votes"] == 1500000

    def test_no_aggregate_rating(self, scraper):
        ld_json = json.dumps({"name": "Some Movie"})
        html = f"""
        <html><head>
        <script type="application/ld+json">{ld_json}</script>
        </head><body></body></html>
        """
        with patch("backend.imdb_scraper.requests") as mock_requests:
            mock_requests.get.return_value = _FakeResponse(html, 200)
            result = scraper.scrape_imdb_data("tt0000001")
        assert result is not None
        assert result["rating"] == 0.0
        assert result["votes"] == 0

    def test_empty_imdb_id_returns_none(self, scraper):
        result = scraper.scrape_imdb_data("")
        assert result is None

    def test_none_imdb_id_returns_none(self, scraper):
        result = scraper.scrape_imdb_data(None)
        assert result is None

    def test_http_error_returns_none(self, scraper):
        with patch("backend.imdb_scraper.requests") as mock_requests:
            mock_requests.get.return_value = _FakeResponse("", 500)
            result = scraper.scrape_imdb_data("tt0000001")
        assert result is None

    def test_network_exception_returns_none(self, scraper):
        with patch("backend.imdb_scraper.requests") as mock_requests:
            mock_requests.get.side_effect = ConnectionError("fail")
            result = scraper.scrape_imdb_data("tt0000001")
        assert result is None

    def test_no_ld_json_script(self, scraper):
        html = "<html><head></head><body><h1>Movie Page</h1></body></html>"
        with patch("backend.imdb_scraper.requests") as mock_requests:
            mock_requests.get.return_value = _FakeResponse(html, 200)
            result = scraper.scrape_imdb_data("tt0000001")
        assert result is not None
        assert result["rating"] == 0.0
        assert result["votes"] == 0


# ===================================================================
# scrape_rt_score — mocked HTTP (napi endpoint)
# ===================================================================

class TestScrapeRtScore:

    def _make_scraper_with_cache(self):
        app = MockApp(config={})
        return WebScrapers(app)

    def test_cached_result_returned(self):
        ws = self._make_scraper_with_cache()
        cache_key = f"rt_scrape_{ws.app.clean_string('Inception')}_{2010}_False"
        ws.app.tmdb_cache[cache_key] = {"critics": 87, "audience": 91}
        result = ws.scrape_rt_score("Inception", year=2010, is_tv=False)
        assert result["critics"] == 87
        assert result["audience"] == 91

    def test_napi_returns_scores(self):
        ws = self._make_scraper_with_cache()
        napi_data = {
            "movies": [
                {
                    "title": "Inception",
                    "releaseYear": 2010,
                    "tomatometerScore": 87,
                    "audienceScore": 91,
                }
            ]
        }
        with patch("backend.rt_scraper.cloudscraper") as mock_cs:
            mock_scraper = MagicMock()
            mock_cs.create_scraper.return_value = mock_scraper
            # napi response
            mock_scraper.get.return_value = _FakeResponse(
                json.dumps(napi_data), 200
            )
            result = ws.scrape_rt_score("Inception", year=2010, is_tv=False)
        assert result["critics"] == 87
        assert result["audience"] == 91

    def test_tv_tries_direct_first(self):
        ws = self._make_scraper_with_cache()
        with patch.object(ws._rt, "_scrape_rt_tv_direct") as mock_direct:
            mock_direct.return_value = {"critics": 95, "audience": 88}
            result = ws.scrape_rt_score("Breaking Bad", year=2008, is_tv=True)
        assert result["critics"] == 95
        assert result["audience"] == 88
        mock_direct.assert_called_once()

    def test_returns_empty_on_all_failures(self):
        ws = self._make_scraper_with_cache()
        with patch("backend.rt_scraper.cloudscraper") as mock_cs:
            mock_scraper = MagicMock()
            mock_cs.create_scraper.return_value = mock_scraper
            # Make all HTTP calls fail
            mock_scraper.get.side_effect = ConnectionError("fail")
            with patch.object(ws._rt, "_scrape_rt_movie_direct") as mock_direct:
                mock_direct.return_value = {"critics": None, "audience": None}
                result = ws.scrape_rt_score("NonExistent", year=2020, is_tv=False)
        assert result["critics"] is None
        assert result["audience"] is None


# ===================================================================
# _title_to_rt_slug — used by RT scraping internals
# ===================================================================

class TestRTSlugIntegration:
    """Test that _title_to_rt_slug produces correct slugs used in RT URLs."""

    def test_slug_no_trailing_underscores(self, scraper):
        slug = scraper._title_to_rt_slug("  Dune  ")
        assert not slug.startswith("_")
        assert not slug.endswith("_")

    def test_slug_lowercase(self, scraper):
        slug = scraper._title_to_rt_slug("THE MATRIX")
        assert slug == "the_matrix"
        assert slug == slug.lower()

    def test_slug_usable_in_url(self, scraper):
        """Slug should only contain word characters and underscores."""
        import re
        slug = scraper._title_to_rt_slug("Fast & Furious: Hobbs! and? Shaw#")
        assert re.match(r'^[\w]+$', slug), f"Slug contains invalid URL chars: {slug}"
