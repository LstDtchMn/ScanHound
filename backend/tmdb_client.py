"""TmdbClient — Unified synchronous TMDB v3 API client.

Provides rate-limited, retrying HTTP access to TMDB endpoints.
Consumers handle caching and business logic; this module handles transport.
"""

import logging
import threading
import time

import requests

from backend.app_service import TMDB_API_BASE

logger = logging.getLogger(__name__)

# Default rate limit: TMDB allows 50 req/10s.  0.20s = 5 req/s.
_DEFAULT_RATE_LIMIT = 0.20


class TmdbClient:
    """Synchronous TMDB v3 API client with rate limiting and retry."""

    def __init__(self, api_key: str, rate_limit: float = _DEFAULT_RATE_LIMIT,
                 timeout: int = 10, max_retries: int = 2):
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._rate_limit_interval = rate_limit
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ── Transport ─────────────────────────────────────────────────────

    def _rate_limit(self):
        """Enforce minimum interval between API calls (thread-safe)."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._rate_limit_interval:
                time.sleep(self._rate_limit_interval - elapsed)
            self._last_call = time.monotonic()

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """GET ``TMDB_API_BASE + path`` with rate limiting and retry.

        Returns parsed JSON dict on success, ``None`` on failure.
        """
        full_params = {"api_key": self.api_key}
        if params:
            full_params.update(params)

        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                resp = requests.get(
                    f"{TMDB_API_BASE}{path}",
                    params=full_params,
                    timeout=self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    logger.warning("TMDB %s failed: HTTP %d after %d retries",
                                   path, resp.status_code, self.max_retries)
                    return None
                if resp.status_code != 200:
                    logger.debug("TMDB %s returned HTTP %d", path, resp.status_code)
                    return None
                return resp.json()
            except (requests.ConnectionError, requests.Timeout):
                if attempt < self.max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                logger.warning("TMDB %s connection failed after %d retries",
                               path, self.max_retries)
            except Exception as e:
                logger.error("TMDB %s unexpected error: %s", path, e)
                break
        return None

    # ── Public API ────────────────────────────────────────────────────

    def search(self, query: str, media_type: str = "movie",
               year: int | None = None, language: str = "en-US") -> list[dict]:
        """Search for movies or TV shows.

        Args:
            query: Search string.
            media_type: ``"movie"`` or ``"tv"``.
            year: Release year filter (optional).
            language: Result language.

        Returns:
            List of result dicts (may be empty).
        """
        params: dict = {"query": query, "language": language}
        if year:
            if media_type == "tv":
                params["first_air_date_year"] = year
            else:
                params["year"] = year

        data = self._get(f"/search/{media_type}", params)
        return data.get("results", []) if data else []

    def details(self, tmdb_id: int, media_type: str = "movie",
                language: str = "en-US") -> dict | None:
        """Get movie or TV details by TMDB ID.

        Returns:
            Full details dict, or ``None`` on failure.
        """
        return self._get(f"/{media_type}/{tmdb_id}", {"language": language})

    def external_ids(self, tmdb_id: int, media_type: str = "movie") -> dict | None:
        """Get external IDs (IMDB, TVDB, etc.) for a movie or TV show.

        Returns:
            Dict with ``imdb_id``, ``tvdb_id``, etc., or ``None``.
        """
        return self._get(f"/{media_type}/{tmdb_id}/external_ids")

    def season(self, tv_id: int, season_number: int) -> dict | None:
        """Get season data including all episodes.

        Returns:
            Season dict with ``episodes`` list, or ``None``.
        """
        return self._get(f"/tv/{tv_id}/season/{season_number}")

    def episode_external_ids(self, tv_id: int, season: int,
                             episode: int) -> dict | None:
        """Get external IDs for a specific episode.

        Returns:
            Dict with ``imdb_id``, etc., or ``None``.
        """
        return self._get(
            f"/tv/{tv_id}/season/{season}/episode/{episode}/external_ids")

    def find(self, external_id: str, source: str = "imdb_id",
             language: str = "en-US") -> dict | None:
        """Find movies/TV via external ID (e.g. IMDB).

        Returns:
            Response dict with ``movie_results``, ``tv_results``, etc.,
            or ``None``.
        """
        return self._get(f"/find/{external_id}", {
            "external_source": source,
            "language": language,
        })
