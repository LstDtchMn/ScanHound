"""RTScraper — Rotten Tomatoes score scraping.

Provides the RTScraper class used by WebScrapers to fetch critics and
audience scores from Rotten Tomatoes.  All methods are synchronous
(blocking) and designed to run in thread-pool executors.
"""

import json
import logging
import re
import random
import time

import cloudscraper
import requests
from bs4 import BeautifulSoup

from backend.models import RTScoreResult

logger = logging.getLogger(__name__)

# User agents rotated for scraping to reduce detection
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
]

# Regex patterns for extracting RT scores from page HTML
_RT_CRITICS_PATTERNS = [
    r'tomatometerscore["\']?\s*[:=]\s*["\']?(\d+)',
    r'tomatometerScore["\']?\s*[:=]\s*["\']?(\d+)',
    r'"tomatoRating":\s*(\d+)',
    r'Critics Consensus.*?(\d+)%',
]
_RT_AUDIENCE_PATTERNS = [
    r'audiencescore["\']?\s*[:=]\s*["\']?(\d+)',
    r'audienceScore["\']?\s*[:=]\s*["\']?(\d+)',
    r'"audienceRating":\s*(\d+)',
]


class RTScraper:
    """Scrapes Rotten Tomatoes critics and audience scores."""

    def __init__(self, parent_app):
        """Initialize with reference to the parent app (config, caches, logging).

        Args:
            parent_app: AppService instance (provides config, tmdb_cache, safe_log).
        """
        self.app = parent_app
        self._scraper = None

    def _get_scraper(self):
        """Get or create a reusable cloudscraper instance."""
        if self._scraper is None:
            self._scraper = cloudscraper.create_scraper()
        return self._scraper

    def _title_to_rt_slug(self, title):
        """Convert a title to a Rotten Tomatoes URL slug.

        Args:
            title: Movie or TV show title.

        Returns:
            str: URL-safe slug (e.g. "the_walking_dead").
        """
        slug = re.sub(r'[^\w\s-]', '', title.lower())
        slug = re.sub(r'[\s-]+', '_', slug)
        return slug.strip('_')

    def _extract_rt_scores_from_page(self, resp) -> RTScoreResult:
        """Extract critics and audience scores from a Rotten Tomatoes page response.

        Tries three extraction strategies in order:
            1. <score-board> custom element attributes (modern RT pages).
            2. JSON-LD structured data embedded in <script> tags.
            3. Regex patterns against the raw HTML.

        Args:
            resp: requests.Response from an RT page (must be status 200).

        Returns:
            dict with 'critics' and 'audience' keys (int or None each).
        """
        result = {'critics': None, 'audience': None}
        soup = BeautifulSoup(resp.content, 'html.parser')

        # Strategy 1: <score-board> custom element (modern RT)
        score_board = soup.find('score-board') or soup.find('score-board-deprecated')
        if score_board:
            for attr, key in [
                ('tomatometerscore', 'critics'),
                ('audiencescore', 'audience'),
            ]:
                raw = score_board.get(attr)
                if raw:
                    try:
                        result[key] = int(raw)
                    except (ValueError, TypeError):
                        pass

        # Strategy 2: JSON-LD structured data
        if result['critics'] is None:
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        rating = data.get('aggregateRating', {})
                        if rating.get('ratingValue'):
                            val = float(rating['ratingValue'])
                            # RT uses 0-100 scale; some pages report 0-10
                            if val <= 10:
                                val = int(val * 10)
                            result['critics'] = int(val)
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

        # Strategy 3: regex patterns against raw HTML
        html = resp.text
        if result['critics'] is None:
            for pattern in _RT_CRITICS_PATTERNS:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    result['critics'] = int(match.group(1))
                    break
        if result['audience'] is None:
            for pattern in _RT_AUDIENCE_PATTERNS:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    result['audience'] = int(match.group(1))
                    break

        return result

    def _build_rt_urls(self, slug, content_type, year=None):
        """Build a list of candidate Rotten Tomatoes URLs to try.

        Args:
            slug: RT-style slug derived from the title.
            content_type: "tv" or "m" (movies).
            year: Release year — used to add year-suffixed URLs for movies.

        Returns:
            List of URL strings to attempt in order.
        """
        urls = [
            f"https://www.rottentomatoes.com/{content_type}/{slug}",
            f"https://www.rottentomatoes.com/{content_type}/{slug.replace('_', '-')}",
        ]
        if year and content_type == "m":
            urls.insert(0, f"https://www.rottentomatoes.com/m/{slug}_{year}")
            urls.append(f"https://www.rottentomatoes.com/m/{slug.replace('_', '-')}_{year}")
        for prefix in ['the_', 'a_', 'an_']:
            if slug.startswith(prefix):
                urls.append(f"https://www.rottentomatoes.com/{content_type}/{slug[len(prefix):]}")
                break
        return urls

    def _scrape_rt_direct(self, title, content_type, year=None):
        """Scrape RT scores by directly accessing the media page.

        Shared implementation for both TV and movie direct scraping.

        Args:
            title: Media title.
            content_type: "tv" or "m" (for URL path segment).
            year: Release year (optional, for URL disambiguation).

        Returns:
            dict: {'critics': int or None, 'audience': int or None}
        """
        result = {'critics': None, 'audience': None}
        slug = self._title_to_rt_slug(title)
        label = "TV" if content_type == "tv" else "Movie"

        if self.app.config.get("debug_mode"):
            self.app.safe_log(f"[RT Direct {label}] Trying slug: {slug}")

        scraper = self._get_scraper()
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.rottentomatoes.com/',
        }
        urls_to_try = self._build_rt_urls(slug, content_type, year)

        for url in urls_to_try:
            try:
                resp = scraper.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    result = self._extract_rt_scores_from_page(resp)
                    if result['critics'] is not None or result['audience'] is not None:
                        if self.app.config.get("debug_mode"):
                            self.app.safe_log(
                                f"[RT Direct {label}] Found at {url}: "
                                f"Critics={result['critics']}, Audience={result['audience']}")
                        return result
            except Exception as e:
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[RT Direct {label}] Error for {url}: {e}")
                continue

        return result

    def _scrape_rt_tv_direct(self, title, year=None):
        """Scrape RT scores by directly accessing the TV show page."""
        return self._scrape_rt_direct(title, "tv", year)

    def _scrape_rt_movie_direct(self, title, year=None):
        """Scrape RT scores by directly accessing the movie page."""
        return self._scrape_rt_direct(title, "m", year)

    def scrape_rt_score(self, title, year=None, is_tv=False) -> RTScoreResult:
        """Scrape Rotten Tomatoes directly for scores (fallback when OMDb fails).

        Attempts four strategies in order:
            1. For TV: direct page scraping (more reliable than search).
            2. RT napi search endpoint (modern JSON API).
            3. RT private API v2 search endpoint.
            4. Search results page HTML parsing + regex fallback.
            5. For movies: direct page scraping as final fallback.

        Results are cached in ``self.app.tmdb_cache``.

        Args:
            title: Movie or TV show title.
            year: Release year (optional).
            is_tv: True if TV show, False if movie.

        Returns:
            dict: {'critics': int or None, 'audience': int or None}
                  Both scores are 0-100, or None if not found.
        """
        cache_key = f"rt_scrape_{self.app.clean_string(title)}_{year or 'any'}_{is_tv}"
        if cache_key in self.app.tmdb_cache:
            return self.app.tmdb_cache[cache_key]

        result = {'critics': None, 'audience': None}

        # For TV shows, try direct page scraping first (more reliable)
        if is_tv:
            result = self._scrape_rt_tv_direct(title, year)
            if result['critics'] is not None or result['audience'] is not None:
                self.app.tmdb_cache[cache_key] = result
                return result

        try:
            scraper = self._get_scraper()
            headers = {
                'User-Agent': random.choice(USER_AGENTS),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }

            search_title = re.sub(r'[^\w\s]', '', title).strip()

            # Method 1: Try RT's napi search endpoint (newer)
            try:
                content_type = 'tv' if is_tv else 'movie'
                api_url = f"https://www.rottentomatoes.com/napi/search?query={requests.utils.quote(search_title)}"
                api_resp = scraper.get(api_url, headers=headers, timeout=10)
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[RT napi] Status: {api_resp.status_code} for '{search_title}'")
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    items = data.get('tvSeries' if is_tv else 'movies', [])
                    if not items:
                        items = data.get('tv' if is_tv else 'movie', [])

                    for item in items:
                        item_year = item.get('startYear') or item.get('releaseYear') or item.get('year')
                        if year and item_year:
                            try:
                                if abs(int(item_year) - int(year)) > 1:
                                    continue
                            except (ValueError, TypeError):
                                pass

                        critics = item.get('tomatometerScore') or item.get('meterScore') or item.get('criticsScore')
                        audience = item.get('audienceScore') or item.get('popcornScore')

                        if isinstance(critics, dict):
                            critics = critics.get('score') or critics.get('value')
                        if isinstance(audience, dict):
                            audience = audience.get('score') or audience.get('value')

                        if critics is not None:
                            result['critics'] = int(critics)
                        if audience is not None:
                            result['audience'] = int(audience)

                        if result['critics'] is not None or result['audience'] is not None:
                            self.app.tmdb_cache[cache_key] = result
                            if self.app.config.get("debug_mode"):
                                self.app.safe_log(f"[RT napi] Found: {item.get('title', item.get('name', title))} = Critics: {result['critics']}%, Audience: {result['audience']}%")
                            return result
            except Exception as e:
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[RT napi] Error: {e}")

            # Method 1b: Try older private API endpoint
            try:
                api_url = f"https://www.rottentomatoes.com/api/private/v2.0/search?q={requests.utils.quote(search_title)}&limit=5"
                api_resp = scraper.get(api_url, headers=headers, timeout=10)
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    content_key = 'tvSeries' if is_tv else 'movies'
                    items = data.get(content_key, [])

                    for item in items:
                        item_year = item.get('startYear') or item.get('year')
                        if year and item_year:
                            try:
                                if abs(int(item_year) - int(year)) > 1:
                                    continue
                            except (ValueError, TypeError):
                                pass

                        critics = item.get('meterScore')
                        audience = item.get('audienceScore', {})
                        if isinstance(audience, dict):
                            audience = audience.get('score')

                        if critics is not None:
                            result['critics'] = int(critics)
                        if audience is not None:
                            result['audience'] = int(audience)

                        if result['critics'] is not None or result['audience'] is not None:
                            self.app.tmdb_cache[cache_key] = result
                            if self.app.config.get("debug_mode"):
                                self.app.safe_log(f"[RT API v2] Found: {item.get('name', title)} = Critics: {result['critics']}%, Audience: {result['audience']}%")
                            return result
            except Exception as e:
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[RT API v2] Error: {e}")

            # Method 2: Scrape search results page
            search_url = f"https://www.rottentomatoes.com/search?search={requests.utils.quote(search_title)}"
            resp = scraper.get(search_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return result

            soup = BeautifulSoup(resp.content, 'html.parser')

            for script in soup.find_all('script', type='application/json'):
                try:
                    json_data = json.loads(script.string)
                    if isinstance(json_data, dict):
                        items = json_data.get('items', [])
                        if not items:
                            items = json_data.get('results', []) or json_data.get('data', {}).get('items', [])

                        for item in items:
                            critics = item.get('tomatometerScore', {}).get('score') if isinstance(item.get('tomatometerScore'), dict) else item.get('tomatometerScore')
                            audience = item.get('audienceScore', {}).get('score') if isinstance(item.get('audienceScore'), dict) else item.get('audienceScore')

                            if critics is not None:
                                result['critics'] = int(critics)
                            if audience is not None:
                                result['audience'] = int(audience)

                            if result['critics'] is not None or result['audience'] is not None:
                                self.app.tmdb_cache[cache_key] = result
                                return result
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

            # Method 3: Parse HTML for score elements
            score_patterns = [
                (r'tomatometer.*?(\d+)%', 'critics'),
                (r'audience.*?score.*?(\d+)%', 'audience'),
                (r'"tomatometerScore":\s*(\d+)', 'critics'),
                (r'"audienceScore":\s*(\d+)', 'audience'),
            ]

            html_text = resp.text
            for pattern, score_type in score_patterns:
                if result[score_type] is None:
                    match = re.search(pattern, html_text, re.IGNORECASE)
                    if match:
                        result[score_type] = int(match.group(1))

            if result['critics'] is not None or result['audience'] is not None:
                self.app.tmdb_cache[cache_key] = result
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[RT Scrape] Found: Critics: {result['critics']}%, Audience: {result['audience']}%")
                return result

        except Exception as e:
            if self.app.config.get("debug_mode"):
                self.app.safe_log(f"[RT Scrape] Error for '{title}': {e}")

        # Final fallback: Try direct page scraping for movies
        if not is_tv and result['critics'] is None and result['audience'] is None:
            result = self._scrape_rt_movie_direct(title, year)
            if result['critics'] is not None or result['audience'] is not None:
                self.app.tmdb_cache[cache_key] = result
                return result

        return result
