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
    Optional keys: episode_end (multi-ep file end), part (split file part number),
    aka (alternate/English title), imdb_id (tt-id embedded in the filename).
    """
    title: str
    year: Optional[int]
    season: Optional[int]
    episode: Optional[int]
    episode_end: Optional[int]
    resolution: Optional[str]
    is_tv: bool
    filename_episode_title: str
    part: Optional[int]
    aka: Optional[str]
    imdb_id: Optional[str]


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
    # End of a glued multi-episode range parsed from THE release filename
    # (S01E01E03 -> episode_number=1, episode_end=3). Never derived from a
    # season pack's file count -- a pack has episode_number None and no range.
    episode_end: Optional[int]
    episodes: Optional[int]
    # Codec evidence, positive-only: True when the filename carries an exact
    # HEVC/x265/H.265 token, None otherwise (absence is not a claim of H.264).
    hevc: Optional[bool]
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
