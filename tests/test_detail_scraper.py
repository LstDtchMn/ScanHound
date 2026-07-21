"""Tests for backend/detail_scraper.py — DetailScraper."""

import pytest
from unittest.mock import MagicMock, patch

from backend.detail_scraper import DetailScraper


class MockApp:
    def __init__(self, debug=False):
        self.config = {"debug_mode": debug}

    def safe_log(self, msg):
        pass

    def clean_string(self, s):
        import re
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9\s]", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def parse_size(self, s):
        import re
        if not s or s == "?":
            return 0.0
        s_up = s.upper().replace(" ", "")
        val_str = re.sub(r"[A-Z]+", "", s_up)
        try:
            val = float(val_str)
        except ValueError:
            return 0.0
        if "GB" in s_up or "GIB" in s_up:
            return val
        if "MB" in s_up or "MIB" in s_up:
            return val / 1024
        return val


def make_scraper(debug=False):
    return DetailScraper(MockApp(debug=debug))


def make_mock_scraper(status_code=200, content=b""):
    """Build a mock cloudscraper instance."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = content
    mock_cs = MagicMock()
    mock_cs.get.return_value = mock_resp
    return mock_cs


MOVIE_HTML = b"""<html><body>
<div class="entry-content">
Filename....: The.Dark.Knight.2008.1080p.BluRay.x265.mkv
Rating : 9.0
File Size: 12.5 GB
Resolution : 1920x1080
<a href="https://www.imdb.com/title/tt0468569/">IMDb</a>
</div>
</body></html>"""

TV_EPISODE_HTML = b"""<html><body>
<div class="entry-content">
Filename....: Breaking.Bad.S01E03.Tuco.1080p.WEB-DL.mkv
Rating : 9.5
File Size: 2.1 GB
Resolution : 1920x1080
</div>
</body></html>"""

SEASON_PACK_HTML = b"""<html><body>
<div class="entry-content">
Filename....: Breaking.Bad.S01E01.1080p.mkv
Filename....: Breaking.Bad.S01E02.1080p.mkv
Filename....: Breaking.Bad.S01E03.1080p.mkv
File Size: 15 GB
</div>
</body></html>"""

TV_SEASON_ONLY_HTML = b"""<html><body>
<div class="entry-content">
Filename....: House.Of.The.Dragon.S02.1080p.WEB-DL.mkv
File Size: 20 GB
</div>
</body></html>"""

DV_HTML = b"""<html><body>
<div class="entry-content">
Filename....: Avatar.2009.2160p.BluRay.DV.mkv
File Size: 55 GB
</div>
</body></html>"""

NO_FILENAME_HTML = b"""<html><body>
<div class="entry-content">
<p>Some page without a filename field.</p>
</div>
</body></html>"""


# ── Basic failure cases ───────────────────────────────────────────────

class TestScrapeDetailsFailures:

    def test_returns_none_on_no_filename_in_page(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/post", {},
            scraper=make_mock_scraper(200, NO_FILENAME_HTML)
        )
        assert result is None

    def test_returns_none_on_non_200_after_retries(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/post", {},
            scraper=make_mock_scraper(404, b"Not Found")
        )
        assert result is None

    def test_returns_none_on_429_after_retries(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/post", {},
            scraper=make_mock_scraper(429, b"Rate limited")
        )
        assert result is None

    def test_returns_none_on_connection_exception(self):
        scraper = make_scraper()
        mock_cs = MagicMock()
        mock_cs.get.side_effect = Exception("connection refused")
        result = scraper.scrape_details("https://example.com/post", {}, scraper=mock_cs)
        assert result is None


class TestScrapeDetailsTimeout:
    """B4: the detail scraper's per-request HTTP call must enforce a timeout
    (like the 15s listing crawl in scanner_service.py), so a single hung
    detail page can't wedge a scan indefinitely."""

    def test_request_passes_a_timeout_kwarg(self):
        scraper = make_scraper()
        mock_cs = make_mock_scraper(200, MOVIE_HTML)
        scraper.scrape_details("https://example.com/post", {}, scraper=mock_cs)
        mock_cs.get.assert_called_once()
        _, kwargs = mock_cs.get.call_args
        assert "timeout" in kwargs, "DetailScraper.scrape_details() made an HTTP request with no timeout"
        assert isinstance(kwargs["timeout"], (int, float)) and kwargs["timeout"] > 0

    def test_timeout_applied_on_every_retry_attempt(self):
        """Even a 404/retry-triggering response must still carry a timeout on
        every attempt, not just the first."""
        scraper = make_scraper()
        mock_cs = make_mock_scraper(404, b"Not Found")
        scraper.scrape_details("https://example.com/post", {}, scraper=mock_cs)
        assert mock_cs.get.call_count >= 1
        for call in mock_cs.get.call_args_list:
            _, kwargs = call
            assert kwargs.get("timeout"), "a retry attempt was made without a timeout"


# ── Movie parsing ─────────────────────────────────────────────────────

class TestScrapeDetailsMovie:

    def test_title_extracted(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result is not None
        assert result["display_title"] == "The Dark Knight"

    def test_year_extracted(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["year"] == 2008

    def test_imdb_id_extracted(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["imdb_id"] == "tt0468569"

    def test_imdb_link_extracted(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["imdb_link"] is not None
        assert "imdb.com" in result["imdb_link"]

    def test_size_extracted(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["size"] != "?"
        assert "12.5" in result["size"] or "GB" in result["size"]

    def test_resolution_from_filename_1080p(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["res"] == "1080p"

    def test_is_tv_false_for_movie(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["is_tv"] is False

    def test_url_stored_in_result(self):
        scraper = make_scraper()
        url = "https://example.com/dark-knight"
        result = scraper.scrape_details(url, {}, scraper=make_mock_scraper(200, MOVIE_HTML))
        assert result["url"] == url

    def test_4k_resolution_from_filename(self):
        html = b"""<html><body>
<div class="entry-content">
Filename....: Film.2020.2160p.UHD.BluRay.mkv
File Size: 60 GB
</div>
</body></html>"""
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com", {},
            scraper=make_mock_scraper(200, html)
        )
        assert result["res"] == "4K"

    def test_dv_flag_detected(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/avatar", {},
            scraper=make_mock_scraper(200, DV_HTML)
        )
        assert result is not None
        assert result["dovi"] is True

    def test_no_dv_flag_by_default(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        assert result["dovi"] is False


# ── TV episode parsing ────────────────────────────────────────────────

class TestScrapeDetailsTvEpisode:

    def test_is_tv_true(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb", {},
            scraper=make_mock_scraper(200, TV_EPISODE_HTML)
        )
        assert result["is_tv"] is True

    def test_season_number(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb", {},
            scraper=make_mock_scraper(200, TV_EPISODE_HTML)
        )
        assert result["season"] == 1

    def test_episode_number(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb", {},
            scraper=make_mock_scraper(200, TV_EPISODE_HTML)
        )
        assert result["episode_number"] == 3

    def test_tv_title_extracted(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb", {},
            scraper=make_mock_scraper(200, TV_EPISODE_HTML)
        )
        assert result["display_title"] == "Breaking Bad"

    def test_tv_season_only_match(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/hotd", {},
            scraper=make_mock_scraper(200, TV_SEASON_ONLY_HTML)
        )
        assert result is not None
        assert result["is_tv"] is True
        assert result["season"] == 2
        assert result["episode_number"] is None


# ── Season pack ───────────────────────────────────────────────────────

class TestScrapeDetailsSeasonPack:

    def test_episode_number_none_for_pack(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb-s1", {},
            scraper=make_mock_scraper(200, SEASON_PACK_HTML)
        )
        assert result is not None
        # Multiple unique episodes → treated as season pack → episode_number=None
        assert result["episode_number"] is None

    def test_episodes_count_correct(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb-s1", {},
            scraper=make_mock_scraper(200, SEASON_PACK_HTML)
        )
        # 3 unique episode numbers (E01, E02, E03)
        assert result["episodes"] == 3

    def test_is_tv_true_for_pack(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/bb-s1", {},
            scraper=make_mock_scraper(200, SEASON_PACK_HTML)
        )
        assert result["is_tv"] is True


# ── Uses provided scraper ─────────────────────────────────────────────

class TestScrapeDetailsScraperParam:

    def test_uses_provided_scraper_not_new_one(self):
        """When scraper= is provided, cloudscraper.create_scraper should not be called."""
        scraper = make_scraper()
        mock_cs = make_mock_scraper(200, MOVIE_HTML)

        with patch("backend.detail_scraper.create_source_http_client") as mock_create:
            scraper.scrape_details("https://example.com", {}, scraper=mock_cs)
            mock_create.assert_not_called()

    def test_creates_scraper_when_none_provided(self):
        """When scraper=None, a new cloudscraper should be created."""
        scraper = make_scraper()
        mock_cs = make_mock_scraper(200, MOVIE_HTML)

        with patch(
            "backend.detail_scraper.create_source_http_client",
            return_value=mock_cs,
        ) as mock_create:
            result = scraper.scrape_details("https://example.com", {})
            mock_create.assert_called_once_with(hdencode=True)
            assert result is not None


# ── Result structure ──────────────────────────────────────────────────

class TestScrapeDetailsResultStructure:

    def test_all_expected_keys_present(self):
        scraper = make_scraper()
        result = scraper.scrape_details(
            "https://example.com/dark-knight", {},
            scraper=make_mock_scraper(200, MOVIE_HTML)
        )
        expected_keys = {
            "display_title", "year", "rating", "search_key", "url",
            "imdb_link", "imdb_id", "size", "res", "hdr", "dovi",
            "tmdb_votes", "is_tv", "season", "episode_number", "episodes",
            "posted_date", "multi_episode_hint",
        }
        assert expected_keys.issubset(result.keys())


# ── Page hint extraction ──────────────────────────────────────────────────

class TestPageHintExtraction:

    def test_extracts_combined_hint_from_page_text(self):
        html = b"""<html><body>
<div class="entry-content">
Filename....: Show.S01E01.1080p.mkv
File Size: 2 GB
<p>This is a double episode release.</p>
</div>
</body></html>"""
        scraper = make_scraper()
        result = scraper.scrape_details(
            "http://fake-url/post/123",
            {},
            scraper=make_mock_scraper(200, html)
        )
        assert result is not None
        hints = result.get("multi_episode_hint")
        assert hints is not None
        assert hints["is_combined"] is True

    def test_returns_none_hint_for_normal_page(self):
        html = b"""<html><body>
<div class="entry-content">
Filename....: Show.S01E01.1080p.mkv
File Size: 2 GB
<p>Great episode, action-packed.</p>
</div>
</body></html>"""
        scraper = make_scraper()
        result = scraper.scrape_details(
            "http://fake-url/post/456",
            {},
            scraper=make_mock_scraper(200, html)
        )
        assert result is not None
        hints = result.get("multi_episode_hint")
        assert hints is None
