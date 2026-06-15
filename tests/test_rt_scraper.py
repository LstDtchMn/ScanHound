"""Tests for backend/rt_scraper.py — RTScraper class.

Covers the uncovered 64% of rt_scraper.py:
- _title_to_rt_slug: slug generation edge cases
- _extract_rt_scores_from_page: all three strategies (score-board, JSON-LD, regex)
- _build_rt_urls: movie/TV with year, article prefix stripping, hyphen variant
- _scrape_rt_direct: happy path and 404 path (via mocked cloudscraper)
- scrape_rt_score: API v2 endpoint path, search HTML page path, movie direct fallback
"""

import json
import pytest
from unittest.mock import MagicMock, patch, call

import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.rt_scraper import RTScraper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(debug=False):
    app = MagicMock()
    app.config = {"debug_mode": debug}
    app.tmdb_cache = {}
    app.clean_string = lambda s: s.lower().replace(" ", "").replace("'", "")
    return app


def _make_scraper(debug=False):
    return RTScraper(_make_app(debug=debug))


class _FakeResponse:
    def __init__(self, body="", status_code=200):
        self.status_code = status_code
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.text = body if isinstance(body, str) else body.decode("utf-8")

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# _title_to_rt_slug
# ---------------------------------------------------------------------------

class TestTitleToRtSlug:

    def test_basic_title(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("The Matrix") == "the_matrix"

    def test_colon_removed(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("Spider-Man: No Way Home") == "spider_man_no_way_home"

    def test_leading_trailing_spaces_stripped(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("  Dune  ") == "dune"

    def test_numbers_preserved(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("2001: A Space Odyssey") == "2001_a_space_odyssey"

    def test_empty_string(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("") == ""

    def test_apostrophes_removed(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("Schindler's List") == "schindlers_list"

    def test_ampersand_removed(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("Fast & Furious") == "fast_furious"

    def test_multiple_spaces_collapsed(self):
        s = _make_scraper()
        assert s._title_to_rt_slug("The   Big   Lebowski") == "the_big_lebowski"

    def test_underscore_not_duplicated(self):
        s = _make_scraper()
        result = s._title_to_rt_slug("Alien (Director's Cut)")
        # no double-underscores
        assert "__" not in result


# ---------------------------------------------------------------------------
# _build_rt_urls
# ---------------------------------------------------------------------------

class TestBuildRtUrls:

    def test_movie_base_urls_included(self):
        s = _make_scraper()
        urls = s._build_rt_urls("the_matrix", "m")
        assert "https://www.rottentomatoes.com/m/the_matrix" in urls

    def test_hyphen_variant_included(self):
        s = _make_scraper()
        urls = s._build_rt_urls("spider_man", "m")
        assert any("spider-man" in u for u in urls)

    def test_movie_year_url_prepended(self):
        s = _make_scraper()
        urls = s._build_rt_urls("inception", "m", year=2010)
        assert "https://www.rottentomatoes.com/m/inception_2010" in urls
        # Year-suffixed URL should appear before the plain one
        year_idx = urls.index("https://www.rottentomatoes.com/m/inception_2010")
        plain_idx = urls.index("https://www.rottentomatoes.com/m/inception")
        assert year_idx < plain_idx

    def test_movie_year_hyphen_variant_appended(self):
        s = _make_scraper()
        urls = s._build_rt_urls("the_matrix", "m", year=1999)
        assert any("the-matrix_1999" in u for u in urls)

    def test_tv_no_year_suffix(self):
        s = _make_scraper()
        urls = s._build_rt_urls("breaking_bad", "tv", year=2008)
        assert not any("_2008" in u for u in urls)
        assert "https://www.rottentomatoes.com/tv/breaking_bad" in urls

    def test_article_the_prefix_stripped(self):
        s = _make_scraper()
        urls = s._build_rt_urls("the_godfather", "m")
        assert any("rottentomatoes.com/m/godfather" in u for u in urls)

    def test_article_a_prefix_stripped(self):
        s = _make_scraper()
        urls = s._build_rt_urls("a_beautiful_mind", "m")
        assert any("rottentomatoes.com/m/beautiful_mind" in u for u in urls)

    def test_article_an_prefix_stripped(self):
        s = _make_scraper()
        urls = s._build_rt_urls("an_american_werewolf", "m")
        assert any("rottentomatoes.com/m/american_werewolf" in u for u in urls)

    def test_no_article_no_extra_url(self):
        s = _make_scraper()
        # slug does not start with the_, a_, or an_
        urls_without = s._build_rt_urls("inception", "m")
        urls_with = s._build_rt_urls("the_inception", "m")
        # the_inception gets one more URL (stripped)
        assert len(urls_with) > len(urls_without)


# ---------------------------------------------------------------------------
# _extract_rt_scores_from_page
# ---------------------------------------------------------------------------

class TestExtractRtScoresFromPage:

    def _resp(self, html):
        return _FakeResponse(html)

    # Strategy 1: <score-board>
    def test_score_board_critics_and_audience(self):
        html = '<score-board tomatometerscore="92" audiencescore="88"></score-board>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] == 92
        assert result["audience"] == 88

    def test_score_board_deprecated_element(self):
        html = '<score-board-deprecated tomatometerscore="75"></score-board-deprecated>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] == 75

    def test_score_board_invalid_value_skipped(self):
        html = '<score-board tomatometerscore="N/A" audiencescore="80"></score-board>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] is None
        assert result["audience"] == 80

    # Strategy 2: JSON-LD structured data
    def test_json_ld_0_to_100_scale(self):
        data = {"aggregateRating": {"ratingValue": 88.0}}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] == 88

    def test_json_ld_0_to_10_scale_multiplied(self):
        data = {"aggregateRating": {"ratingValue": 8.5}}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] == 85

    def test_json_ld_malformed_skipped(self):
        html = '<script type="application/ld+json">NOT JSON</script>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] is None

    def test_json_ld_no_aggregate_rating_skipped(self):
        data = {"@type": "Movie", "name": "Inception"}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] is None

    # Strategy 3: regex patterns
    def test_regex_critics_lowercase(self):
        html = 'tomatometerscore: 70'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] == 70

    def test_regex_audience_lowercase(self):
        html = 'audiencescore: 65'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["audience"] == 65

    def test_regex_camelcase_audience(self):
        html = 'audienceScore: 72'
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["audience"] == 72

    def test_no_scores_returns_none(self):
        html = "<html><body>No scores here.</body></html>"
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] is None
        assert result["audience"] is None

    def test_score_board_takes_precedence_over_json_ld(self):
        # score-board gives 90, json-ld would give 50
        data = {"aggregateRating": {"ratingValue": 50}}
        html = (
            f'<score-board tomatometerscore="90"></score-board>'
            f'<script type="application/ld+json">{json.dumps(data)}</script>'
        )
        s = _make_scraper()
        result = s._extract_rt_scores_from_page(self._resp(html))
        assert result["critics"] == 90


# ---------------------------------------------------------------------------
# _scrape_rt_direct
# ---------------------------------------------------------------------------

class TestScrapeRtDirect:

    def test_returns_scores_on_200(self):
        html = '<score-board tomatometerscore="88" audiencescore="77"></score-board>'
        s = _make_scraper()

        mock_scraper = MagicMock()
        mock_scraper.get.return_value = _FakeResponse(html, 200)

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s._scrape_rt_direct("Inception", "m", year=2010)

        assert result["critics"] == 88
        assert result["audience"] == 77

    def test_skips_404_responses(self):
        s = _make_scraper()
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = _FakeResponse("Not Found", 404)

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s._scrape_rt_direct("NoSuchMovie", "m")

        assert result["critics"] is None
        assert result["audience"] is None

    def test_exception_per_url_continues_to_next(self):
        s = _make_scraper()
        good_html = '<score-board tomatometerscore="75"></score-board>'
        mock_scraper = MagicMock()
        # First call raises, second returns valid HTML
        mock_scraper.get.side_effect = [
            Exception("connection refused"),
            _FakeResponse(good_html, 200),
        ]

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s._scrape_rt_direct("Test Movie", "m")

        assert result["critics"] == 75

    def test_tv_uses_tv_content_type(self):
        s = _make_scraper()
        called_urls = []

        def fake_get(url, headers=None, timeout=None):
            called_urls.append(url)
            return _FakeResponse("", 404)

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = fake_get

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            s._scrape_rt_tv_direct("Breaking Bad", year=2008)

        assert all("/tv/" in u for u in called_urls)

    def test_movie_uses_m_content_type(self):
        s = _make_scraper()
        called_urls = []

        def fake_get(url, headers=None, timeout=None):
            called_urls.append(url)
            return _FakeResponse("", 404)

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = fake_get

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            s._scrape_rt_movie_direct("The Matrix", year=1999)

        assert all("/m/" in u for u in called_urls)


# ---------------------------------------------------------------------------
# scrape_rt_score — additional paths not covered by existing tests
# ---------------------------------------------------------------------------

class TestScrapeRtScoreUncoveredPaths:

    def test_api_v2_endpoint_used_as_fallback(self):
        """When napi returns 200 but no matching items, v2 should be tried."""
        s = _make_scraper()

        # napi: 200 but empty movies list
        napi_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        # v2 API: returns a match
        v2_data = {
            "movies": [{"name": "Inception", "year": 2010, "meterScore": 87, "audienceScore": 91}]
        }
        v2_resp = _FakeResponse(json.dumps(v2_data), 200)
        # search page: 404 (prevents further processing)
        search_resp = _FakeResponse("", 404)

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = [napi_resp, v2_resp, search_resp]

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s.scrape_rt_score("Inception", year=2010, is_tv=False)

        assert result["critics"] == 87

    def test_year_mismatch_skips_item(self):
        """Items with year more than 1 off should be skipped."""
        s = _make_scraper()

        napi_data = {
            "movies": [
                {"tomatometerScore": 99, "audienceScore": 99, "releaseYear": 2005},
            ]
        }
        napi_resp = _FakeResponse(json.dumps(napi_data), 200)
        search_resp = _FakeResponse("", 404)
        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = [napi_resp, search_resp]

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper), \
             patch.object(s, "_scrape_rt_movie_direct", return_value={"critics": None, "audience": None}):
            result = s.scrape_rt_score("Inception", year=2010, is_tv=False)

        # Year is off by 5 — item should be skipped, so no score from napi
        assert result["critics"] is None

    def test_audience_score_as_dict_extracted(self):
        """audienceScore as dict with 'score' key is unwrapped."""
        s = _make_scraper()

        v2_data = {
            "movies": [{
                "name": "Movie",
                "meterScore": 80,
                "audienceScore": {"score": 75},
            }]
        }
        # napi empty, v2 returns dict-form audience
        napi_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        v2_resp = _FakeResponse(json.dumps(v2_data), 200)
        search_resp = _FakeResponse("", 404)

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = [napi_resp, v2_resp, search_resp]

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s.scrape_rt_score("Movie", is_tv=False)

        assert result["critics"] == 80
        assert result["audience"] == 75

    def test_search_page_json_script_parsed(self):
        """Falls through to search page JSON parsing when APIs fail."""
        s = _make_scraper()

        json_in_page = json.dumps({
            "items": [
                {"tomatometerScore": 70, "audienceScore": 60}
            ]
        })
        search_html = f'<script type="application/json">{json_in_page}</script>'

        napi_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        v2_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        search_resp = _FakeResponse(search_html, 200)

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = [napi_resp, v2_resp, search_resp]

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s.scrape_rt_score("Some Film", is_tv=False)

        assert result["critics"] == 70

    def test_search_page_regex_fallback(self):
        """HTML regex patterns used when JSON parsing finds nothing."""
        s = _make_scraper()

        search_html = 'Tomatometer score: 65% | Audience Score: 58%'

        napi_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        v2_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        search_resp = _FakeResponse(search_html, 200)

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = [napi_resp, v2_resp, search_resp]

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s.scrape_rt_score("Mediocre Film", is_tv=False)

        assert result["critics"] == 65

    def test_movie_direct_final_fallback(self):
        """Direct page scraping is last resort for movies."""
        s = _make_scraper()

        napi_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        v2_resp = _FakeResponse(json.dumps({"movies": []}), 200)
        search_resp = _FakeResponse("nothing useful", 200)  # no JSON, no regex match

        mock_scraper = MagicMock()
        mock_scraper.get.side_effect = [napi_resp, v2_resp, search_resp]

        direct_result = {"critics": 82, "audience": 78}
        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper), \
             patch.object(s, "_scrape_rt_movie_direct", return_value=direct_result) as mock_direct:
            result = s.scrape_rt_score("Great Film", year=2020, is_tv=False)

        mock_direct.assert_called_once_with("Great Film", 2020)
        assert result["critics"] == 82

    def test_result_cached_after_successful_lookup(self):
        s = _make_scraper()

        napi_data = {"movies": [{"tomatometerScore": 91, "audienceScore": 85}]}
        napi_resp = _FakeResponse(json.dumps(napi_data), 200)

        mock_scraper = MagicMock()
        mock_scraper.get.return_value = napi_resp

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            s.scrape_rt_score("Cached Film", year=2021, is_tv=False)

        cache_key = f"rt_scrape_{s.app.clean_string('Cached Film')}_2021_False"
        assert cache_key in s.app.tmdb_cache
        assert s.app.tmdb_cache[cache_key]["critics"] == 91

    def test_napi_tvSeries_key_used_for_tv(self):
        """TV shows use 'tvSeries' key in napi response."""
        s = _make_scraper()

        napi_data = {
            "tvSeries": [{"tomatometerScore": 95, "audienceScore": 90, "startYear": 2008}]
        }
        napi_resp = _FakeResponse(json.dumps(napi_data), 200)

        mock_scraper = MagicMock()
        mock_scraper.get.return_value = napi_resp

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper), \
             patch.object(s, "_scrape_rt_tv_direct", return_value={"critics": None, "audience": None}):
            result = s.scrape_rt_score("Breaking Bad", year=2008, is_tv=True)

        assert result["critics"] == 95

    def test_critics_score_as_dict_extracted(self):
        """tomatometerScore as dict {'score': N} is unwrapped."""
        s = _make_scraper()

        napi_data = {
            "movies": [{"tomatometerScore": {"score": 77}, "audienceScore": 66}]
        }
        napi_resp = _FakeResponse(json.dumps(napi_data), 200)
        mock_scraper = MagicMock()
        mock_scraper.get.return_value = napi_resp

        with patch("backend.rt_scraper.cloudscraper.create_scraper", return_value=mock_scraper):
            result = s.scrape_rt_score("Film", is_tv=False)

        assert result["critics"] == 77
