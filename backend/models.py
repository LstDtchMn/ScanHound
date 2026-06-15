"""Shared TypedDict definitions for ScanHound backend.

These types replace plain dict returns in the public APIs of the scraping
and parsing modules, making the key contracts explicit and IDE-checkable.

Importing
---------
    from backend.models import FilenameResult, ScrapeResult, IMDbData, RTScoreResult
"""

from typing import Optional
from typing_extensions import TypedDict


class FilenameResult(TypedDict, total=False):
    """Return type of :func:`filename_utils.parse_filename`.

    Core keys (always present): title, year, season, episode, resolution, is_tv.
    Optional key (TV only): filename_episode_title — text extracted from the
    filename between the SxxExx token and the first quality/release tag.
    """
    title: str
    year: Optional[int]
    season: Optional[int]
    episode: Optional[int]
    resolution: Optional[str]
    is_tv: bool
    filename_episode_title: str


class ScrapeResult(TypedDict):
    """Return type of :meth:`detail_scraper.DetailScraper.scrape_details`.

    The method returns ``None`` on failure, or a fully-populated
    ``ScrapeResult`` on success.  All keys are always present.
    """
    display_title: str
    year: int
    rating: str
    search_key: str
    url: str
    imdb_link: Optional[str]
    imdb_id: Optional[str]
    size: str
    res: str
    hdr: str
    dovi: bool
    tmdb_votes: str
    is_tv: bool
    season: Optional[int]
    episode_number: Optional[int]
    episodes: Optional[int]
    posted_date: Optional[str]


class IMDbData(TypedDict):
    """Return type of :meth:`imdb_scraper.IMDbScraper.scrape_imdb_data`.

    The method returns ``None`` on failure, or an ``IMDbData`` dict on
    success.
    """
    rating: float
    votes: int


class RTScoreResult(TypedDict):
    """Return type of :meth:`rt_scraper.RTScraper.scrape_rt_score` and
    :meth:`rt_scraper.RTScraper._extract_rt_scores_from_page`.

    Both scores are integers in the range 0–100, or ``None`` when the
    value could not be determined.
    """
    critics: Optional[int]
    audience: Optional[int]
