"""HDEncode Source - Implementation of HDEncode.org scraper as a source plugin."""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup

from .base import (
    SourceBase,
    SourceConfig,
    SourceCapability,
    ParsedRelease,
    PageResult
)

logger = logging.getLogger(__name__)


class HDEncodeSource(SourceBase):
    """HDEncode.org source implementation."""

    BASE_URL = "https://hdencode.org"

    # URL patterns for different content types
    URL_PATTERNS = {
        'movies': '/category/movies/',
        'movies_1080p': '/category/movies/1080p/',
        'movies_4k': '/category/movies/4k-uhd/',
        'tv': '/category/tv-shows/',
        'tv_1080p': '/category/tv-shows/1080p-tv-shows/',
        'tv_4k': '/category/tv-shows/4k-uhd-tv-shows/',
    }

    @classmethod
    def get_config(cls) -> SourceConfig:
        """Return HDEncode configuration."""
        return SourceConfig(
            name="hdencode",
            display_name="HDEncode",
            base_url=cls.BASE_URL,
            capabilities=(
                SourceCapability.MOVIES |
                SourceCapability.TV_SHOWS |
                SourceCapability.PAGINATION |
                SourceCapability.SEARCH
            ),
            rate_limit=2.0,  # Be respectful
            requires_cloudflare_bypass=False,
            timeout=30,
            priority=100
        )

    def __init__(self):
        """Initialize HDEncode source."""
        super().__init__()
        self._session = None  # aiohttp session

    async def fetch_page(
        self,
        page: int = 1,
        mode: str = "movies",
        resolution: Optional[str] = None,
        **kwargs
    ) -> PageResult:
        """Fetch a page of releases from HDEncode.

        Args:
            page: Page number (1-indexed)
            mode: "movies", "tv", or "all"
            resolution: Optional filter by resolution ("1080p", "4K")

        Returns:
            PageResult with releases
        """
        releases = []
        errors = []

        # Determine URL based on mode and resolution
        if mode == "movies":
            if resolution == "4K":
                url_path = self.URL_PATTERNS['movies_4k']
            elif resolution == "1080p":
                url_path = self.URL_PATTERNS['movies_1080p']
            else:
                url_path = self.URL_PATTERNS['movies']
        elif mode == "tv":
            if resolution == "4K":
                url_path = self.URL_PATTERNS['tv_4k']
            elif resolution == "1080p":
                url_path = self.URL_PATTERNS['tv_1080p']
            else:
                url_path = self.URL_PATTERNS['tv']
        else:
            # "all" mode - fetch both
            movies_result = await self.fetch_page(page, "movies", resolution)
            tv_result = await self.fetch_page(page, "tv", resolution)
            return PageResult(
                releases=movies_result.releases + tv_result.releases,
                has_next=movies_result.has_next or tv_result.has_next,
                errors=movies_result.errors + tv_result.errors
            )

        # Add pagination
        if page > 1:
            url = f"{self.BASE_URL}{url_path}page/{page}/"
        else:
            url = f"{self.BASE_URL}{url_path}"

        has_next = False
        try:
            # Fetch page (run in executor for blocking I/O)
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, url)

            if not html:
                errors.append(f"Failed to fetch {url}")
                return PageResult(releases=[], errors=errors)

            # Parse releases
            soup = BeautifulSoup(html, 'html.parser')
            articles = soup.select('article.post, div.post, .type-post')

            for article in articles:
                try:
                    release = self._parse_article(article, mode)
                    if release:
                        releases.append(release)
                except Exception as e:
                    logger.debug("Failed to parse article: %s", e)
                    continue

            # Check for next page
            has_next = self._has_next_page(soup)

            logger.debug("Fetched %s releases from %s", len(releases), url)

        except Exception as e:
            logger.error("Error fetching HDEncode page: %s", e)
            errors.append(str(e))

        return PageResult(
            releases=releases,
            current_page=page,
            has_next=has_next,
            errors=errors
        )

    # _fetch_html inherited from SourceBase (retry-aware)

    def _parse_article(self, article: Any, mode: str) -> Optional[ParsedRelease]:
        """Parse an article element into a ParsedRelease."""
        try:
            # Find title and link
            title_elem = article.select_one('h2 a, h1 a, .entry-title a, .post-title a')
            if not title_elem:
                return None

            title = title_elem.get_text(strip=True)
            url = title_elem.get('href', '')

            if not title or not url:
                return None

            # Parse the title for metadata
            return self.parse_release({
                'title': title,
                'url': url,
                'mode': mode,
                'article_html': str(article)
            })

        except Exception as e:
            logger.debug("Error parsing article: %s", e)
            return None

    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        """Parse raw data into a ParsedRelease."""
        if isinstance(raw_data, dict):
            title = raw_data.get('title', '')
            url = raw_data.get('url', '')
            mode = raw_data.get('mode', 'movies')
            article_html = raw_data.get('article_html', '')
        else:
            return None

        if not title:
            return None

        # Extract metadata from title
        year = self.extract_year(title)
        resolution = self.extract_resolution(title)
        size_str, size_bytes = self.extract_size(title + ' ' + article_html)
        season, episode = self.extract_season_episode(title)
        is_hdr, is_dovi, hdr_format = self.extract_hdr_info(title)
        codec = self.extract_codec(title)
        audio = self.extract_audio_codec(title)

        # Determine if TV
        is_tv = mode == 'tv' or self.is_tv_release(title)

        # Clean title
        display_title = self._extract_display_title(title, year)

        # Check for season pack
        is_season_pack = season is not None and episode is None

        # Try to extract IMDb ID from article HTML
        imdb_id = self.extract_imdb_id(article_html) if article_html else None

        # Detect remux/web
        is_remux = 'remux' in title.lower()
        is_web = 'web-dl' in title.lower() or 'webdl' in title.lower() or 'webrip' in title.lower()

        # Extract release group
        release_group = self._extract_release_group(title)

        return ParsedRelease(
            title=title,
            url=url,
            source="hdencode",
            display_title=display_title,
            year=year,
            resolution=resolution,
            size=size_str,
            size_bytes=size_bytes,
            is_hdr=is_hdr,
            is_dovi=is_dovi,
            hdr_format=hdr_format,
            codec=codec,
            audio_codec=audio,
            is_remux=is_remux,
            is_web=is_web,
            release_group=release_group,
            imdb_id=imdb_id,
            is_tv=is_tv,
            season=season,
            episode=episode,
            is_season_pack=is_season_pack,
            raw_data=raw_data
        )

    def _extract_display_title(self, title: str, year: int) -> str:
        """Extract clean display title."""
        # Remove everything after year if present
        if year:
            idx = title.find(str(year))
            if idx > 0:
                title = title[:idx + 4].strip()

        # Remove common tags
        title = re.sub(
            r'\b(720p|1080p|2160p|4K|UHD|BluRay|BDRip|WEB-?DL|WEB-?Rip|'
            r'REMUX|HDR10?\+?|Dolby\s*Vision|DV)\b.*$',
            '',
            title,
            flags=re.IGNORECASE
        )

        # Clean up
        title = re.sub(r'[._]', ' ', title)
        title = re.sub(r'\s+', ' ', title)

        return title.strip()

    def _extract_release_group(self, title: str) -> str:
        """Extract release group from title."""
        return super()._extract_release_group(title)

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        """Check if there's a next page."""
        # Look for pagination
        next_link = soup.select_one('a.next, .nav-next a, .pagination .next')
        if next_link:
            return True

        # Look for page numbers
        pages = soup.select('.page-numbers, .pagination a')
        if pages:
            current = soup.select_one('.current, .page-numbers.current')
            if current:
                try:
                    current_num = int(current.get_text(strip=True))
                    for page in pages:
                        try:
                            num = int(page.get_text(strip=True))
                            if num > current_num:
                                return True
                        except ValueError:
                            continue
                except ValueError:
                    pass

        return False

    async def search(
        self,
        query: str,
        mode: str = "all",
        **kwargs
    ) -> PageResult:
        """Search HDEncode.

        The search results page uses a different HTML layout than category
        pages: each result is a ``.fit.item`` div with ``h5 > a`` for the
        title/link, so we cannot reuse ``_parse_article`` which expects
        ``article`` tags with ``h2 > a``.
        """
        url = f"{self.BASE_URL}/?s={query.replace(' ', '+')}"

        try:
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, url)

            if not html:
                return PageResult(releases=[], errors=["Search returned no HTML — site may be blocking."])

            soup = BeautifulSoup(html, 'html.parser')

            # Search results use .fit.item > .data > h5 > a
            items = soup.select('.fit.item')

            releases = []
            for item in items:
                try:
                    link = item.select_one('.data h5 a, h5 a')
                    if not link:
                        continue
                    title = link.get_text(strip=True)
                    item_url = link.get('href', '')
                    if not title or not item_url:
                        continue

                    release = self.parse_release({
                        'title': title,
                        'url': item_url,
                        'mode': mode,
                        'article_html': str(item),
                    })
                    if release:
                        if mode == "movies" and release.is_tv:
                            continue
                        if mode == "tv" and not release.is_tv:
                            continue
                        releases.append(release)
                except Exception as e:
                    logger.debug("Failed to parse search result: %s", e)
                    continue

            return PageResult(
                releases=releases,
                has_next=self._has_next_page(soup)
            )

        except Exception as e:
            logger.error("Search error: %s", e)
            return PageResult(releases=[], errors=[str(e)])

    async def fetch_release_details(self, url: str) -> Optional[ParsedRelease]:
        """Fetch full details for a release."""
        try:
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, url)

            if not html:
                return None

            soup = BeautifulSoup(html, 'html.parser')

            # Get title
            title_elem = soup.select_one('h1.entry-title, .post-title, h1')
            title = title_elem.get_text(strip=True) if title_elem else ''

            # Get content for more details
            content = soup.select_one('.entry-content, .post-content, article')
            content_text = content.get_text() if content else ''
            content_html = str(content) if content else ''

            # Parse as usual but with more data
            release = self.parse_release({
                'title': title,
                'url': url,
                'mode': 'movies',
                'article_html': content_html
            })

            if release and content:
                # Try to extract additional details
                # Screenshots
                screenshots = [
                    img.get('src') for img in content.select('img')
                    if img.get('src') and 'screenshot' in img.get('src', '').lower()
                ]
                if screenshots:
                    release.screenshots = screenshots[:5]

                # Description (usually in a specific div or first paragraph)
                desc_elem = content.select_one('.plot, .description, p')
                if desc_elem:
                    release.description = desc_elem.get_text(strip=True)[:500]

                # Try harder to get IMDb ID
                if not release.imdb_id:
                    imdb_link = content.select_one('a[href*="imdb.com/title/"]')
                    if imdb_link:
                        release.imdb_id = self.extract_imdb_id(imdb_link.get('href', ''))

            return release

        except Exception as e:
            logger.error("Error fetching release details: %s", e)
            return None

    async def fetch_download_links(
        self,
        release: ParsedRelease,
        service: str = "rapidgator",
    ) -> List[str]:
        """Fail closed: browser retrieval is owned by DownloadService.

        This removes the dormant independent WebDriver constructor and its
        automation-obscuring options. Callers must use the coordinated API path.
        """
        logger.warning(
            "HDEncodeSource.fetch_download_links is disabled; "
            "use DownloadService through the coordinated API path"
        )
        return []
