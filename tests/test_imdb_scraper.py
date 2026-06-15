"""Tests for backend/imdb_scraper.py — IMDbScraper."""

import json
import pytest
from unittest.mock import MagicMock, patch

from backend.imdb_scraper import IMDbScraper


class MockApp:
    def __init__(self, debug=False):
        self.config = {"debug_mode": debug}
    def safe_log(self, msg):
        pass


def make_scraper(debug=False):
    return IMDbScraper(MockApp(debug=debug))


def make_response(status_code=200, content=b"<html></html>"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    return resp


def html_with_ld_json(data: dict) -> bytes:
    """Build minimal IMDb-style HTML with an ld+json script block."""
    json_str = json.dumps(data)
    return (
        f'<html><body>'
        f'<script type="application/ld+json">{json_str}</script>'
        f'</body></html>'
    ).encode("utf-8")


# ── Guard on imdb_id ─────────────────────────────────────────────────

class TestScrapeImdbDataGuards:

    def test_returns_none_for_empty_string(self):
        result = make_scraper().scrape_imdb_data("")
        assert result is None

    def test_returns_none_for_none(self):
        result = make_scraper().scrape_imdb_data(None)
        assert result is None

    def test_returns_none_for_false(self):
        result = make_scraper().scrape_imdb_data(False)
        assert result is None


# ── Happy path ────────────────────────────────────────────────────────

class TestScrapeImdbDataSuccess:

    def test_returns_rating_and_votes(self):
        html = html_with_ld_json({
            "aggregateRating": {"ratingValue": 8.8, "ratingCount": 2500000}
        })
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert result is not None
        assert result["rating"] == 8.8
        assert result["votes"] == 2500000

    def test_votes_with_comma_parsed(self):
        html = html_with_ld_json({
            "aggregateRating": {"ratingValue": 7.5, "ratingCount": "1,250,000"}
        })
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0000001")
        assert result["votes"] == 1250000

    def test_rating_returned_as_float(self):
        html = html_with_ld_json({
            "aggregateRating": {"ratingValue": 9, "ratingCount": 1000}
        })
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0000001")
        assert isinstance(result["rating"], float)

    def test_correct_url_called(self):
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200)) as mock_get:
            make_scraper().scrape_imdb_data("tt0133093")
        url = mock_get.call_args[0][0]
        assert "tt0133093" in url
        assert "imdb.com" in url

    def test_result_dict_has_rating_and_votes_keys(self):
        html = html_with_ld_json({
            "aggregateRating": {"ratingValue": 8.0, "ratingCount": 100000}
        })
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert "rating" in result
        assert "votes" in result


# ── No aggregate rating data ──────────────────────────────────────────

class TestScrapeImdbDataMissingData:

    def test_no_ld_json_script_returns_zero_defaults(self):
        html = b"<html><body><p>No script here</p></body></html>"
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        # Still returns dict (with 0 defaults), not None
        assert result is not None
        assert result["rating"] == 0.0
        assert result["votes"] == 0

    def test_ld_json_without_aggregate_rating(self):
        html = html_with_ld_json({"@type": "Movie", "name": "Some Movie"})
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert result["rating"] == 0.0
        assert result["votes"] == 0

    def test_malformed_ld_json_does_not_crash(self):
        html = b'<html><script type="application/ld+json">{invalid json</script></html>'
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(200, html)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        # Should still return a result (with 0s), not crash
        assert result is not None


# ── Error conditions ──────────────────────────────────────────────────

class TestScrapeImdbDataErrors:

    def test_returns_none_on_non_200_status(self):
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(403)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert result is None

    def test_returns_none_on_404(self):
        with patch("backend.imdb_scraper.requests.get", return_value=make_response(404)):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert result is None

    def test_returns_none_on_network_exception(self):
        with patch("backend.imdb_scraper.requests.get", side_effect=Exception("network error")):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert result is None

    def test_returns_none_on_timeout(self):
        import requests
        with patch("backend.imdb_scraper.requests.get", side_effect=requests.Timeout()):
            result = make_scraper().scrape_imdb_data("tt0133093")
        assert result is None
