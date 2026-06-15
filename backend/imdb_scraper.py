"""IMDbScraper — Direct IMDb rating scraping.

Provides the IMDbScraper class used by WebScrapers to fetch ratings and
vote counts from IMDb when OMDb returns N/A.  All methods are synchronous
(blocking) and designed to run in thread-pool executors.
"""

import json
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup

from backend.models import IMDbData

logger = logging.getLogger(__name__)


class IMDbScraper:
    """Scrapes IMDb directly for rating and vote data."""

    def __init__(self, parent_app):
        """Initialize with reference to the parent app (config, logging).

        Args:
            parent_app: AppService instance (provides config, safe_log).
        """
        self.app = parent_app

    def scrape_imdb_data(self, imdb_id) -> Optional[IMDbData]:
        """Scrape IMDb directly for rating and votes when OMDb returns N/A.

        Extracts data from the JSON-LD structured data block embedded in the
        IMDb title page, which is more stable than HTML scraping.

        Args:
            imdb_id: IMDb ID string (e.g. 'tt0133093').

        Returns:
            dict: {'rating': float, 'votes': int} or None if failed.
        """
        if not imdb_id:
            return None

        try:
            url = f"https://www.imdb.com/title/{imdb_id}/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9'
            }
            resp = requests.get(url, headers=headers, timeout=10)

            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, 'html.parser')

                rating = 0.0
                votes = 0

                # Extract rating from JSON-LD structured data
                script_tag = soup.find('script', type='application/ld+json')
                if script_tag:
                    try:
                        ld_json = json.loads(script_tag.string)
                        if 'aggregateRating' in ld_json:
                            rating = float(ld_json['aggregateRating'].get('ratingValue', 0))
                            votes_str = str(ld_json['aggregateRating'].get('ratingCount', 0))
                            votes = int(votes_str.replace(',', ''))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        pass

                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[IMDb Scrape] {imdb_id}: Rating={rating}, Votes={votes}")

                return {"rating": rating, "votes": votes}

        except Exception as e:
            if self.app.config.get("debug_mode"):
                self.app.safe_log(f"[IMDb Scrape] Error for {imdb_id}: {e}")

        return None
