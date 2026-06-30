"""Comprehensive tests for backend/tmdb_client.py module.

Covers:
- TmdbClient initialization and configuration
- Rate limiting logic
- _get transport: success, retries on 429/5xx, connection errors, unexpected errors
- Public API methods: search, details, external_ids, season, episode_external_ids, find
- Edge cases: None params, empty responses, various HTTP status codes
"""

import os
import sys
import time
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.tmdb_client import TmdbClient, _DEFAULT_RATE_LIMIT


# ======================================================================
# 1. Initialization
# ======================================================================

class TestTmdbClientInit:
    """Tests for TmdbClient constructor."""

    def test_default_params(self):
        client = TmdbClient(api_key="test_key")
        assert client.api_key == "test_key"
        assert client.timeout == 10
        assert client.max_retries == 2
        assert client._rate_limit_interval == _DEFAULT_RATE_LIMIT

    def test_custom_params(self):
        client = TmdbClient(api_key="key2", rate_limit=0.5, timeout=30, max_retries=5)
        assert client.api_key == "key2"
        assert client._rate_limit_interval == 0.5
        assert client.timeout == 30
        assert client.max_retries == 5

    def test_initial_last_call_zero(self):
        client = TmdbClient(api_key="key")
        assert client._last_call == 0.0

    def test_has_lock(self):
        client = TmdbClient(api_key="key")
        assert isinstance(client._lock, type(threading.Lock()))

    def test_default_rate_limit_constant(self):
        assert _DEFAULT_RATE_LIMIT == 0.20


# ======================================================================
# 2. Rate Limiting
# ======================================================================

class TestRateLimit:
    """Tests for _rate_limit method."""

    def test_first_call_no_sleep(self):
        client = TmdbClient(api_key="key", rate_limit=0.20)
        client._last_call = 0.0
        with patch("backend.tmdb_client.time.sleep") as mock_sleep:
            with patch("backend.tmdb_client.time.monotonic", side_effect=[100.0, 100.0]):
                client._rate_limit()
                mock_sleep.assert_not_called()

    def test_sleep_when_too_fast(self):
        client = TmdbClient(api_key="key", rate_limit=0.20)
        client._last_call = 100.0
        with patch("backend.tmdb_client.time.sleep") as mock_sleep:
            with patch("backend.tmdb_client.time.monotonic", side_effect=[100.05, 100.25]):
                client._rate_limit()
                mock_sleep.assert_called_once()
                # Should sleep for ~0.15 seconds (0.20 - 0.05)
                sleep_arg = mock_sleep.call_args[0][0]
                assert 0.10 < sleep_arg < 0.20

    def test_no_sleep_when_enough_elapsed(self):
        client = TmdbClient(api_key="key", rate_limit=0.20)
        client._last_call = 99.0
        with patch("backend.tmdb_client.time.sleep") as mock_sleep:
            with patch("backend.tmdb_client.time.monotonic", side_effect=[100.0, 100.0]):
                client._rate_limit()
                mock_sleep.assert_not_called()

    def test_updates_last_call(self):
        client = TmdbClient(api_key="key", rate_limit=0.20)
        with patch("backend.tmdb_client.time.monotonic", side_effect=[200.0, 200.0]):
            client._rate_limit()
        assert client._last_call == 200.0


# ======================================================================
# 3. _get Transport
# ======================================================================

class TestGet:
    """Tests for _get method — core HTTP transport with retries."""

    def _make_client(self, max_retries=2):
        client = TmdbClient(api_key="test_api_key", rate_limit=0.0, max_retries=max_retries)
        client._last_call = 0.0
        return client

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_success_200(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": [{"id": 1}]}
        mock_get.return_value = mock_resp

        result = client._get("/search/movie", {"query": "Test"})
        assert result == {"results": [{"id": 1}]}
        mock_get.assert_called_once()
        # Verify api_key is in the params
        call_kwargs = mock_get.call_args
        assert "api_key" in call_kwargs[1]["params"] or "api_key" in call_kwargs.kwargs.get("params", {})

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_params_include_api_key(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp

        client._get("/test", {"foo": "bar"})
        call_args = mock_get.call_args
        params = call_args[1]["params"] if "params" in call_args[1] else call_args.kwargs["params"]
        assert params["api_key"] == "test_api_key"
        assert params["foo"] == "bar"

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_params_none(self, mock_get, mock_sleep):
        """When params is None, only api_key should be sent."""
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp

        client._get("/test")
        call_args = mock_get.call_args
        params = call_args[1]["params"] if "params" in call_args[1] else call_args.kwargs["params"]
        assert params == {"api_key": "test_api_key"}

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_404_returns_none(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = client._get("/movie/999999")
        assert result is None
        # Should not retry on 404
        assert mock_get.call_count == 1

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_429_retries_then_fails(self, mock_get, mock_sleep):
        client = self._make_client(max_retries=2)
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp

        result = client._get("/search/movie")
        assert result is None
        # 1 initial + 2 retries = 3 calls
        assert mock_get.call_count == 3

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_500_retries_then_fails(self, mock_get, mock_sleep):
        client = self._make_client(max_retries=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        result = client._get("/search/movie")
        assert result is None
        assert mock_get.call_count == 2  # 1 initial + 1 retry

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_503_retries_then_fails(self, mock_get, mock_sleep):
        client = self._make_client(max_retries=1)
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_get.return_value = mock_resp

        result = client._get("/test")
        assert result is None
        assert mock_get.call_count == 2

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_429_then_success(self, mock_get, mock_sleep):
        """429 on first attempt, then success on retry."""
        client = self._make_client(max_retries=2)

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"results": []}

        mock_get.side_effect = [mock_resp_429, mock_resp_200]

        result = client._get("/search/movie")
        assert result == {"results": []}
        assert mock_get.call_count == 2

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_connection_error_retries(self, mock_get, mock_sleep):
        import requests as req
        client = self._make_client(max_retries=2)
        mock_get.side_effect = req.ConnectionError("Connection refused")

        result = client._get("/search/movie")
        assert result is None
        assert mock_get.call_count == 3

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_timeout_error_retries(self, mock_get, mock_sleep):
        import requests as req
        client = self._make_client(max_retries=1)
        mock_get.side_effect = req.Timeout("Request timed out")

        result = client._get("/test")
        assert result is None
        assert mock_get.call_count == 2

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_connection_error_then_success(self, mock_get, mock_sleep):
        import requests as req
        client = self._make_client(max_retries=2)

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {"id": 42}

        mock_get.side_effect = [req.ConnectionError("fail"), mock_resp_200]

        result = client._get("/test")
        assert result == {"id": 42}
        assert mock_get.call_count == 2

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_unexpected_error_breaks_immediately(self, mock_get, mock_sleep):
        client = self._make_client(max_retries=2)
        mock_get.side_effect = ValueError("Something unexpected")

        result = client._get("/test")
        assert result is None
        # Should not retry on unexpected errors
        assert mock_get.call_count == 1

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_max_retries_zero(self, mock_get, mock_sleep):
        """With max_retries=0, no retries should happen."""
        client = self._make_client(max_retries=0)
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp

        result = client._get("/test")
        assert result is None
        assert mock_get.call_count == 1

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_retry_backoff_timing(self, mock_get, mock_sleep):
        """Verify that retry sleeps use increasing backoff."""
        client = self._make_client(max_retries=2)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        client._get("/test")
        # time.sleep is called by rate_limit + retry backoff
        # Retry backoff: 1.0 * (attempt+1), so 1.0 then 2.0
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        # Should contain 1.0 and 2.0 from retry backoff
        assert 1.0 in sleep_calls
        assert 2.0 in sleep_calls


# ======================================================================
# 4. Public API - search
# ======================================================================

class TestSearch:
    """Tests for TmdbClient.search method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_movie_success(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"id": 1, "title": "Test Movie"}]
        }
        mock_get.return_value = mock_resp

        results = client.search("Test Movie")
        assert len(results) == 1
        assert results[0]["title"] == "Test Movie"

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_tv_success(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{"id": 2, "name": "Test Show"}]
        }
        mock_get.return_value = mock_resp

        results = client.search("Test Show", media_type="tv")
        assert len(results) == 1
        # Verify the path includes /search/tv
        call_url = mock_get.call_args[0][0]
        assert "/search/tv" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_with_year_movie(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_get.return_value = mock_resp

        client.search("Movie", year=2024)
        params = mock_get.call_args[1]["params"]
        assert params["year"] == 2024
        assert "first_air_date_year" not in params

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_with_year_tv(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_get.return_value = mock_resp

        client.search("Show", media_type="tv", year=2020)
        params = mock_get.call_args[1]["params"]
        assert params["first_air_date_year"] == 2020
        assert "year" not in params

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_no_year(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_get.return_value = mock_resp

        client.search("Movie", year=None)
        params = mock_get.call_args[1]["params"]
        assert "year" not in params
        assert "first_air_date_year" not in params

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_with_language(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_get.return_value = mock_resp

        client.search("Film", language="de-DE")
        params = mock_get.call_args[1]["params"]
        assert params["language"] == "de-DE"

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_api_failure_returns_empty(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        results = client.search("Movie")
        assert results == []

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_no_results_key(self, mock_get, mock_sleep):
        """API returns 200 but no 'results' key."""
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"total_results": 0}
        mock_get.return_value = mock_resp

        results = client.search("Missing")
        assert results == []

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_search_empty_results(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_get.return_value = mock_resp

        results = client.search("Nonexistent Movie 12345")
        assert results == []


# ======================================================================
# 5. Public API - details
# ======================================================================

class TestDetails:
    """Tests for TmdbClient.details method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_movie_details_success(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 550, "title": "Fight Club"}
        mock_get.return_value = mock_resp

        result = client.details(550)
        assert result == {"id": 550, "title": "Fight Club"}
        call_url = mock_get.call_args[0][0]
        assert "/movie/550" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_tv_details_success(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 1399, "name": "Breaking Bad"}
        mock_get.return_value = mock_resp

        result = client.details(1399, media_type="tv")
        assert result["name"] == "Breaking Bad"
        call_url = mock_get.call_args[0][0]
        assert "/tv/1399" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_details_with_language(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 550}
        mock_get.return_value = mock_resp

        client.details(550, language="fr-FR")
        params = mock_get.call_args[1]["params"]
        assert params["language"] == "fr-FR"

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_details_failure_returns_none(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = client.details(9999999)
        assert result is None


# ======================================================================
# 6. Public API - external_ids
# ======================================================================

class TestExternalIds:
    """Tests for TmdbClient.external_ids method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_movie_external_ids(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"imdb_id": "tt0137523", "tvdb_id": None}
        mock_get.return_value = mock_resp

        result = client.external_ids(550)
        assert result["imdb_id"] == "tt0137523"
        call_url = mock_get.call_args[0][0]
        assert "/movie/550/external_ids" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_tv_external_ids(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"imdb_id": "tt0903747", "tvdb_id": 81189}
        mock_get.return_value = mock_resp

        result = client.external_ids(1399, media_type="tv")
        assert result["tvdb_id"] == 81189
        call_url = mock_get.call_args[0][0]
        assert "/tv/1399/external_ids" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_external_ids_failure(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = client.external_ids(9999999)
        assert result is None


class TestCredits:
    """Tests for TmdbClient.credits method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_movie_credits(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "cast": [{"name": "Adam Sandler"}],
            "crew": [{"name": "Peter Segal", "job": "Director"}]}
        mock_get.return_value = mock_resp

        result = client.credits(550)
        assert result["cast"][0]["name"] == "Adam Sandler"
        assert "/movie/550/credits" in mock_get.call_args[0][0]

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_tv_credits_url(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cast": [], "crew": []}
        mock_get.return_value = mock_resp

        client.credits(1399, media_type="tv")
        assert "/tv/1399/credits" in mock_get.call_args[0][0]


# ======================================================================
# 7. Public API - season
# ======================================================================

class TestSeason:
    """Tests for TmdbClient.season method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_season_success(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "season_number": 1,
            "episodes": [{"episode_number": 1}, {"episode_number": 2}]
        }
        mock_get.return_value = mock_resp

        result = client.season(1399, 1)
        assert len(result["episodes"]) == 2
        call_url = mock_get.call_args[0][0]
        assert "/tv/1399/season/1" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_season_failure(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = client.season(1399, 99)
        assert result is None


# ======================================================================
# 8. Public API - episode_external_ids
# ======================================================================

class TestEpisodeExternalIds:
    """Tests for TmdbClient.episode_external_ids method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_episode_external_ids_success(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"imdb_id": "tt0959621"}
        mock_get.return_value = mock_resp

        result = client.episode_external_ids(1399, 1, 1)
        assert result["imdb_id"] == "tt0959621"
        call_url = mock_get.call_args[0][0]
        assert "/tv/1399/season/1/episode/1/external_ids" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_episode_external_ids_failure(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = client.episode_external_ids(1399, 99, 99)
        assert result is None


# ======================================================================
# 9. Public API - find
# ======================================================================

class TestFind:
    """Tests for TmdbClient.find method."""

    def _make_client(self):
        return TmdbClient(api_key="key", rate_limit=0.0, max_retries=0)

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_find_by_imdb_id(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "movie_results": [{"id": 550, "title": "Fight Club"}],
            "tv_results": []
        }
        mock_get.return_value = mock_resp

        result = client.find("tt0137523")
        assert len(result["movie_results"]) == 1
        call_url = mock_get.call_args[0][0]
        assert "/find/tt0137523" in call_url

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_find_with_custom_source(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"movie_results": [], "tv_results": []}
        mock_get.return_value = mock_resp

        client.find("81189", source="tvdb_id")
        params = mock_get.call_args[1]["params"]
        assert params["external_source"] == "tvdb_id"

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_find_with_language(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp

        client.find("tt0137523", language="de-DE")
        params = mock_get.call_args[1]["params"]
        assert params["language"] == "de-DE"

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_find_failure(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        result = client.find("invalid_id")
        assert result is None

    @patch("backend.tmdb_client.time.sleep")
    @patch("backend.tmdb_client.requests.get")
    def test_find_default_source_is_imdb(self, mock_get, mock_sleep):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp

        client.find("tt0137523")
        params = mock_get.call_args[1]["params"]
        assert params["external_source"] == "imdb_id"
