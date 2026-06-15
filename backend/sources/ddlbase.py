"""DDLBase Source - Implementation of DDLBase.com scraper as a source plugin.

Scrapes remux movie releases from DDLBase and extracts 1fichier.com download links
via cuty.io shortlinks.
"""

import asyncio
import base64
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


def decode_ddlbase_link(encoded: str, key: str = "mySecret123") -> Optional[str]:
    """Decode a DDLBase ``ddllk`` attribute to its actual URL.

    DDLBase XOR-encrypts shortlink URLs with a fixed key then base64-encodes
    the result.  This reverses the process.
    """
    try:
        # Normalise base64 padding — DDLBase may strip trailing '='
        padded = encoded + "=" * (-len(encoded) % 4)
        data = base64.b64decode(padded)
        url = "".join(
            chr(byte ^ ord(key[i % len(key)])) for i, byte in enumerate(data)
        )
        if url.startswith("http"):
            return url
        logger.debug("Decoded ddllk does not look like URL: %s", url[:60])
        return None
    except Exception as e:
        logger.warning("Failed to decode ddllk '%s': %s", encoded[:20], e)
        return None


class DDLBaseSource(SourceBase):
    """DDLBase.com source implementation for remux movies."""

    BASE_URL = "https://ddlbase.com"

    # URL patterns for different content types
    URL_PATTERNS = {
        'movies_1080p': '/cat/movie-remux-1080p/',
        'movies_4k': '/cat/movie-remux-2160p/',
        'movies_webdl_4k': '/cat/movie-webdl-2160p/',
    }

    @classmethod
    def get_config(cls) -> SourceConfig:
        """Return DDLBase configuration."""
        return SourceConfig(
            name="ddlbase",
            display_name="DDLBase",
            base_url=cls.BASE_URL,
            capabilities=(
                SourceCapability.MOVIES |
                SourceCapability.PAGINATION |
                SourceCapability.SEARCH |
                SourceCapability.DIRECT_LINKS |
                SourceCapability.CLOUDFLARE_BYPASS
            ),
            rate_limit=2.0,  # Be respectful
            requires_cloudflare_bypass=True,
            timeout=30,
            priority=90,
            enabled=True
        )

    async def fetch_page(
        self,
        page: int = 1,
        mode: str = "movies",
        resolution: Optional[str] = None,
        **kwargs
    ) -> PageResult:
        """Fetch a page of releases from DDLBase.

        Args:
            page: Page number (1-indexed)
            mode: "movies" (only mode supported)
            resolution: Optional filter by resolution ("1080p", "4K")

        Returns:
            PageResult with releases
        """
        releases = []
        errors = []

        # Determine URL based on resolution
        if resolution == "4K" or resolution == "2160p":
            url_path = self.URL_PATTERNS['movies_4k']
        elif resolution == "1080p":
            url_path = self.URL_PATTERNS['movies_1080p']
        else:
            # Default: fetch both and combine
            result_1080p = await self.fetch_page(page, mode, "1080p")
            result_4k = await self.fetch_page(page, mode, "4K")
            return PageResult(
                releases=result_1080p.releases + result_4k.releases,
                has_next=result_1080p.has_next or result_4k.has_next,
                errors=result_1080p.errors + result_4k.errors
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

            # DDLBase uses article elements for posts
            articles = soup.select('article, div.post, .type-post, .hentry')

            for article in articles:
                try:
                    release = self._parse_article(article, resolution or "1080p")
                    if release:
                        releases.append(release)
                except Exception as e:
                    logger.debug("Failed to parse article: %s", e)
                    continue

            # Check for next page
            has_next = self._has_next_page(soup)

            logger.debug("Fetched %s releases from %s", len(releases), url)

        except Exception as e:
            logger.error("Error fetching DDLBase page: %s", e)
            errors.append(str(e))

        return PageResult(
            releases=releases,
            current_page=page,
            has_next=has_next,
            errors=errors
        )

    # _fetch_html inherited from SourceBase (retry-aware)

    async def search(
        self,
        query: str,
        mode: str = "all",
        **kwargs
    ) -> PageResult:
        """Search DDLBase via WordPress site search."""
        url = f"{self.BASE_URL}/?s={query.replace(' ', '+')}"

        try:
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, url)

            if not html:
                return PageResult(releases=[], errors=["Search failed"])

            soup = BeautifulSoup(html, 'html.parser')
            articles = soup.select('article, div.post, .type-post, .hentry')

            releases = []
            for article in articles:
                try:
                    release = self._parse_article(article, "1080p")
                    if release:
                        releases.append(release)
                except Exception as e:
                    logger.debug("Failed to parse search result: %s", e)
                    continue

            return PageResult(
                releases=releases,
                has_next=self._has_next_page(soup)
            )

        except Exception as e:
            logger.error("DDLBase search error: %s", e)
            return PageResult(releases=[], errors=[str(e)])

    def _parse_article(self, article: Any, default_resolution: str) -> Optional[ParsedRelease]:
        """Parse an article element into a ParsedRelease."""
        try:
            # Find title and link - try multiple selectors
            title_elem = (
                article.select_one('h2 a') or
                article.select_one('h1 a') or
                article.select_one('.entry-title a') or
                article.select_one('.post-title a') or
                article.select_one('a[rel="bookmark"]')
            )

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
                'default_resolution': default_resolution,
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
            default_resolution = raw_data.get('default_resolution', '1080p')
            article_html = raw_data.get('article_html', '')
        else:
            return None

        if not title:
            return None

        # Extract metadata from title
        year = self.extract_year(title)
        resolution = self.extract_resolution(title) or default_resolution
        size_str, size_bytes = self.extract_size(title + ' ' + article_html)
        is_hdr, is_dovi, hdr_format = self.extract_hdr_info(title)
        codec = self.extract_codec(title)
        audio = self.extract_audio_codec(title)

        # Clean title
        display_title = self._extract_display_title(title, year)

        # Detect remux/web
        is_remux = 'remux' in title.lower()
        is_web = 'web-dl' in title.lower() or 'webdl' in title.lower()

        # Extract release group
        release_group = self._extract_release_group(title)

        # Try to extract IMDb ID from article HTML
        imdb_id = self.extract_imdb_id(article_html) if article_html else None

        return ParsedRelease(
            title=title,
            url=url,
            source="ddlbase",
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
            is_tv=False,  # DDLBase remux categories are movies only
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
        next_link = soup.select_one('a.next, .nav-next a, .pagination .next, a[rel="next"]')
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

    async def fetch_release_details(self, url: str) -> Optional[ParsedRelease]:
        """Fetch full details for a release including download links."""
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
            content_html = str(content) if content else ''

            # Parse as usual
            release = self.parse_release({
                'title': title,
                'url': url,
                'default_resolution': '1080p',
                'article_html': content_html
            })

            return release

        except Exception as e:
            logger.error("Error fetching release details: %s", e)
            return None

    async def fetch_download_links(
        self,
        release: ParsedRelease,
        service: str = "1fichier"
    ) -> List[str]:
        """Fetch download links for a release.

        Extracts Mirror 1 cuty.io links that resolve to 1fichier.com.

        Args:
            release: The release to get links for
            service: Target service (default: 1fichier)

        Returns:
            List of cuty.io URLs (to be resolved by the app)
        """
        try:
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, release.url)

            if not html:
                return []

            soup = BeautifulSoup(html, 'html.parser')
            content = soup.select_one('.entry-content, .post-content, article')

            if not content:
                return []

            links = []
            content_html = str(content)

            # Primary: decode XOR-encrypted ddllk attributes on a.boolk elements
            boolk_tags = content.select('a.boolk[ddllk]')
            if boolk_tags:
                for tag in boolk_tags:
                    encoded = tag.get('ddllk', '')
                    if encoded:
                        decoded_url = decode_ddlbase_link(encoded)
                        if decoded_url:
                            links.append(decoded_url)

            # Fallback: regex for visible cuty.io links
            if not links:
                content_text = content.get_text()
                mirror1_match = re.search(
                    r'Mirror\s*1\s*:?\s*(.*?)(?:Mirror\s*2|$)',
                    content_text,
                    re.IGNORECASE | re.DOTALL
                )

                if mirror1_match:
                    cuty_links = re.findall(
                        r'https?://cuty\.io/[a-zA-Z0-9]+',
                        mirror1_match.group(1)
                    )
                    links.extend(cuty_links)

                if not links:
                    all_cuty_links = re.findall(
                        r'https?://cuty\.io/[a-zA-Z0-9]+',
                        content_html
                    )
                    links.extend(all_cuty_links)

            # Also look for direct 1fichier links
            fichier_links = re.findall(
                r'https?://1fichier\.com/\?[a-zA-Z0-9&=]+',
                content_html
            )
            links.extend(fichier_links)

            # Remove duplicates while preserving order
            seen = set()
            unique_links = []
            for link in links:
                if link not in seen:
                    seen.add(link)
                    unique_links.append(link)

            logger.debug("Found %s download links for %s", len(unique_links), release.display_title)
            return unique_links

        except Exception as e:
            logger.error("Error fetching download links: %s", e)
            return []
