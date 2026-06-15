"""Base Source Module - Abstract base class and types for source plugins.

All source plugins must inherit from SourceBase and implement the required methods.
"""

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, Flag, auto
from typing import Any, Dict, List, Optional, Tuple, Pattern

logger = logging.getLogger(__name__)


class SourceCapability(Flag):
    """Capabilities that a source can provide."""
    MOVIES = auto()
    TV_SHOWS = auto()
    PAGINATION = auto()
    SEARCH = auto()
    RSS = auto()
    API = auto()
    IMDB_LOOKUP = auto()
    DIRECT_LINKS = auto()
    CLOUDFLARE_BYPASS = auto()


@dataclass
class SourceConfig:
    """Configuration for a source."""
    name: str
    display_name: str
    base_url: str
    capabilities: SourceCapability = SourceCapability.MOVIES | SourceCapability.TV_SHOWS
    rate_limit: float = 2.0  # requests per second
    requires_auth: bool = False
    requires_cloudflare_bypass: bool = False
    enabled: bool = True
    priority: int = 100  # Higher = higher priority
    custom_headers: Dict[str, str] = field(default_factory=dict)
    timeout: int = 30


@dataclass
class ParsedRelease:
    """A parsed release from a source."""
    # Required fields
    title: str
    url: str
    source: str  # Source name

    # Media info
    display_title: str = ""
    year: int = 0
    resolution: str = ""  # "720p", "1080p", "4K"
    size: str = ""  # "15.5 GB"
    size_bytes: int = 0

    # Quality indicators
    is_hdr: bool = False
    is_dovi: bool = False
    hdr_format: str = ""  # "HDR10", "HDR10+", "DV"
    codec: str = ""  # "x264", "x265", "AV1"
    audio_codec: str = ""  # "DTS-HD MA", "TrueHD", "Atmos"
    is_remux: bool = False
    is_web: bool = False
    release_group: str = ""

    # IDs
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None

    # TV specific
    is_tv: bool = False
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_count: Optional[int] = None
    is_season_pack: bool = False

    # Metadata
    release_date: Optional[datetime] = None
    description: str = ""
    poster_url: str = ""
    screenshots: List[str] = field(default_factory=list)

    # Search/matching
    search_key: str = ""  # Normalized title for matching

    # Raw data
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Post-initialization processing."""
        if not self.display_title:
            self.display_title = self.title

        if not self.search_key:
            self.search_key = self._normalize_title(self.display_title)

    def _normalize_title(self, title: str) -> str:
        """Normalize title for matching."""
        # Remove year in parentheses
        title = re.sub(r'\s*\(\d{4}\)\s*', ' ', title)
        # Remove special characters
        title = re.sub(r'[^\w\s]', '', title)
        # Normalize whitespace
        title = ' '.join(title.split())
        return title.lower()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for compatibility with existing code."""
        return {
            'display_title': self.display_title,
            'url': self.url,
            'year': self.year,
            'res': self.resolution,
            'size': self.size,
            'dovi': self.is_dovi,
            'hdr': self.hdr_format or ('HDR' if self.is_hdr else ''),
            'imdb_id': self.imdb_id,
            'tmdb_id': self.tmdb_id,
            'is_tv': self.is_tv,
            'season': self.season,
            'episode_number': self.episode,
            'episodes': self.episode_count,
            'search_key': self.search_key,
            'source': self.source,
            'codec': self.codec,
            'audio': self.audio_codec,
            'release_group': self.release_group
        }


@dataclass
class PageResult:
    """Result from fetching a page of releases."""
    releases: List[ParsedRelease]
    total_count: Optional[int] = None
    current_page: int = 1
    total_pages: Optional[int] = None
    has_next: bool = False
    next_page_url: Optional[str] = None
    errors: List[str] = field(default_factory=list)


class SourceBase(ABC):
    """Abstract base class for source plugins.

    All source implementations must inherit from this class and implement
    the required abstract methods.

    Example implementation:
        class MySource(SourceBase):
            @classmethod
            def get_config(cls) -> SourceConfig:
                return SourceConfig(
                    name="mysource",
                    display_name="My Source",
                    base_url="https://example.com",
                    capabilities=SourceCapability.MOVIES | SourceCapability.TV_SHOWS
                )

            async def fetch_page(self, page: int, mode: str) -> PageResult:
                # Implementation
                pass

            def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
                # Implementation
                pass
    """

    # Common regex patterns for parsing
    YEAR_PATTERN: Pattern = re.compile(r'\b(19|20)\d{2}\b')
    RESOLUTION_PATTERN: Pattern = re.compile(r'\b(720p|1080p|2160p|4K|UHD)\b', re.IGNORECASE)
    SIZE_PATTERN: Pattern = re.compile(r'(\d+(?:\.\d+)?)\s*(GB|MB|TB)', re.IGNORECASE)
    SEASON_PATTERN: Pattern = re.compile(r'S(\d{1,2})(?:E(\d{1,2}))?', re.IGNORECASE)
    IMDB_PATTERN: Pattern = re.compile(r'tt\d{7,}')
    HDR_PATTERN: Pattern = re.compile(r'\b(HDR10\+?|Dolby[\s.]*Vision|DV|HDR)\b', re.IGNORECASE)
    CODEC_PATTERN: Pattern = re.compile(r'\b(x264|x265|HEVC|H\.?264|H\.?265|AV1|VP9)\b', re.IGNORECASE)
    AUDIO_PATTERN: Pattern = re.compile(
        r'\b(DTS-HD\s*MA|TrueHD|Atmos|DTS-X|DTS|DD\+?|AAC|FLAC|LPCM)\b',
        re.IGNORECASE
    )

    def __init__(self):
        """Initialize the source."""
        self._config = self.get_config()
        self._scraper = None

    def _get_scraper(self):
        """Get or create a cloudscraper instance (shared across all source plugins)."""
        if self._scraper is None:
            try:
                import cloudscraper
                self._scraper = cloudscraper.create_scraper(
                    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
                )
            except ImportError:
                logger.warning("cloudscraper not installed, using requests")
                import requests
                self._scraper = requests.Session()
        return self._scraper

    def _fetch_html(self, url: str) -> Optional[str]:
        """Fetch HTML from URL using cloudscraper with retry.

        Retries once on both exceptions and on retryable HTTP status codes
        (429 Too Many Requests, 5xx Server Errors).
        """
        # Enforce per-source rate limit
        if self._config.rate_limit > 0:
            now = time.monotonic()
            min_interval = 1.0 / self._config.rate_limit
            if hasattr(self, '_last_fetch_time'):
                elapsed = now - self._last_fetch_time
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
            self._last_fetch_time = time.monotonic()

        scraper = self._get_scraper()
        for attempt in range(2):
            try:
                response = scraper.get(url, timeout=self._config.timeout)
                if response.status_code == 200:
                    return response.text
                # Retry on 429 or 5xx; return None immediately on 4xx (except 429)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        logger.debug(
                            "HTTP %d for %s — retrying", response.status_code, url)
                        continue
                logger.warning("HTTP %d for %s", response.status_code, url)
                return None
            except Exception as e:
                if attempt == 0:
                    logger.debug("Retry fetching %s after error: %s", url, e)
                    continue
                logger.error("Failed to fetch %s: %s", url, e)
                return None
        return None

    def _extract_display_title(self, raw_title: str) -> str:
        """Extract clean display title from a release title."""
        title = self.clean_title(raw_title)
        return title if title else raw_title

    def _extract_release_group(self, text: str) -> str:
        """Extract release group from text (usually after last hyphen)."""
        matches = list(re.finditer(r'-([A-Za-z0-9_]+)(?:\[[^\]]*\])?(?=\s|$)', text))
        return matches[-1].group(1) if matches else ''

    def _has_next_page(self, soup) -> bool:
        """Check if there is a next page in pagination.

        Override in subclasses for source-specific pagination logic.
        """
        # Common patterns: "next" link, page numbers
        next_link = soup.select_one('a.next, a.nextpostslink, a[rel="next"], .nav-next a, .pagination .next a')
        return next_link is not None

    @classmethod
    @abstractmethod
    def get_config(cls) -> SourceConfig:
        """Return the source configuration.

        This is a class method so it can be called without instantiating the source.
        """
        pass

    @property
    def name(self) -> str:
        """Get source name."""
        return self._config.name

    @property
    def config(self) -> SourceConfig:
        """Get source configuration."""
        return self._config

    @abstractmethod
    async def fetch_page(
        self,
        page: int = 1,
        mode: str = "movies",
        **kwargs
    ) -> PageResult:
        """Fetch a page of releases.

        Args:
            page: Page number (1-indexed)
            mode: Content mode ("movies", "tv", "all")
            **kwargs: Additional source-specific parameters

        Returns:
            PageResult with releases and pagination info
        """
        pass

    @abstractmethod
    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        """Parse raw data into a ParsedRelease.

        Args:
            raw_data: Raw data from the source (HTML element, JSON, etc.)

        Returns:
            ParsedRelease or None if parsing fails
        """
        pass

    async def fetch_all_pages(
        self,
        mode: str = "movies",
        max_pages: int = 10,
        **kwargs
    ) -> List[ParsedRelease]:
        """Fetch multiple pages of releases.

        Args:
            mode: Content mode
            max_pages: Maximum pages to fetch
            **kwargs: Additional parameters

        Returns:
            List of all releases
        """
        all_releases = []
        page = 1

        while page <= max_pages:
            result = await self.fetch_page(page, mode, **kwargs)
            all_releases.extend(result.releases)

            if not result.has_next:
                break

            page += 1

        return all_releases

    async def search(
        self,
        query: str,
        mode: str = "all",
        **kwargs
    ) -> PageResult:
        """Search for releases.

        Default implementation raises NotImplementedError.
        Override in subclasses that support search.

        Args:
            query: Search query
            mode: Content mode
            **kwargs: Additional parameters

        Returns:
            PageResult with search results
        """
        if SourceCapability.SEARCH not in self._config.capabilities:
            raise NotImplementedError(f"{self.name} does not support search")
        raise NotImplementedError("Subclass must implement search()")

    async def fetch_release_details(
        self,
        url: str
    ) -> Optional[ParsedRelease]:
        """Fetch full details for a specific release.

        Default implementation returns None.
        Override in subclasses that can fetch additional details.

        Args:
            url: Release URL

        Returns:
            ParsedRelease with full details or None
        """
        return None

    async def fetch_download_links(
        self,
        release: ParsedRelease,
        service: str = "rapidgator"
    ) -> List[str]:
        """Fetch download links for a release.

        Default implementation returns empty list.
        Override in subclasses that support direct links.

        Args:
            release: The release to get links for
            service: Preferred hosting service

        Returns:
            List of download URLs
        """
        if SourceCapability.DIRECT_LINKS not in self._config.capabilities:
            return []
        # Subclasses that declare DIRECT_LINKS must override this method.
        raise NotImplementedError(
            f"{self.__class__.__name__} declares DIRECT_LINKS capability "
            "but did not override fetch_download_links()"
        )

    # Helper methods for parsing

    def extract_year(self, text: str) -> int:
        """Extract year from text."""
        match = self.YEAR_PATTERN.search(text)
        return int(match.group()) if match else 0

    def extract_resolution(self, text: str) -> str:
        """Extract resolution from text."""
        match = self.RESOLUTION_PATTERN.search(text)
        if match:
            res = match.group().upper()
            if res in ('2160P', 'UHD'):
                return '4K'
            return res.lower() if res != '4K' else res
        return ''

    def extract_size(self, text: str) -> Tuple[str, int]:
        """Extract size from text. Returns (display string, bytes)."""
        match = self.SIZE_PATTERN.search(text)
        if match:
            value = float(match.group(1))
            unit = match.group(2).upper()

            # Convert to bytes
            multipliers = {'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
            size_bytes = int(value * multipliers.get(unit, 1))

            return f"{value} {unit}", size_bytes

        return '', 0

    def extract_season_episode(self, text: str) -> Tuple[Optional[int], Optional[int]]:
        """Extract season and episode from text."""
        match = self.SEASON_PATTERN.search(text)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2)) if match.group(2) else None
            return season, episode
        return None, None

    def extract_imdb_id(self, text: str) -> Optional[str]:
        """Extract IMDb ID from text or URL."""
        match = self.IMDB_PATTERN.search(text)
        return match.group() if match else None

    def extract_hdr_info(self, text: str) -> Tuple[bool, bool, str]:
        """Extract HDR info. Returns (is_hdr, is_dovi, hdr_format)."""
        match = self.HDR_PATTERN.search(text)
        if match:
            hdr_text = match.group().upper()
            is_dovi = 'DV' in hdr_text or 'DOLBY' in hdr_text or 'VISION' in hdr_text
            return True, is_dovi, hdr_text
        return False, False, ''

    def extract_codec(self, text: str) -> str:
        """Extract video codec from text."""
        match = self.CODEC_PATTERN.search(text)
        if match:
            codec = match.group().upper()
            # Normalize
            if codec in ('H264', 'H.264'):
                return 'x264'
            if codec in ('H265', 'H.265', 'HEVC'):
                return 'x265'
            return codec.lower()
        return ''

    def extract_audio_codec(self, text: str) -> str:
        """Extract audio codec from text."""
        match = self.AUDIO_PATTERN.search(text)
        return match.group() if match else ''

    def clean_title(self, title: str) -> str:
        """Clean release title to extract media name."""
        # Remove common tags
        title = re.sub(
            r'\b(720p|1080p|2160p|4K|UHD|BluRay|BDRip|WEB-?DL|WEB-?Rip|'
            r'HDRip|REMUX|x264|x265|HEVC|H\.?264|H\.?265|DTS|TrueHD|Atmos|'
            r'DD\+?|AAC|HDR10?\+?|Dolby\s*Vision|DV|IMAX|PROPER|REPACK|'
            r'EXTENDED|UNRATED|DIRECTORS?.CUT)\b',
            '',
            title,
            flags=re.IGNORECASE
        )

        # Remove release group (usually at end after hyphen)
        title = re.sub(r'-\w+$', '', title)

        # Remove dots and underscores
        title = re.sub(r'[._]', ' ', title)

        # Remove year at end
        title = re.sub(r'\s*\(?\d{4}\)?$', '', title)

        # Clean up whitespace
        title = ' '.join(title.split())

        return title.strip()

    def is_tv_release(self, text: str) -> bool:
        """Check if release is TV content."""
        # Check for season/episode pattern
        if self.SEASON_PATTERN.search(text):
            return True

        # Check for common TV indicators
        tv_indicators = [
            r'\bS\d{1,2}\b',
            r'\bSeason\s*\d+\b',
            r'\bComplete\s*Series\b',
            r'\bMini\s*Series\b',
            r'\bTV\s*Series\b'
        ]

        for pattern in tv_indicators:
            if re.search(pattern, text, re.IGNORECASE):
                return True

        return False
