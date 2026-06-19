"""Adit-HD Source - MyBB Forum scraper for Movies and TV Shows.

Scrapes thread listings and post content from adit-hd.com forum.
Supports login authentication and auto-reply for hidden content.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup

from .base import (
    SourceBase,
    SourceConfig,
    SourceCapability,
    ParsedRelease,
    PageResult
)

logger = logging.getLogger(__name__)


class AditHDSource(SourceBase):
    """Adit-HD.com forum source implementation."""

    BASE_URL = "https://www.adit-hd.com"
    LOGIN_URL = "https://www.adit-hd.com/member.php"

    # Forum section URLs
    URL_PATTERNS = {
        'movies': '/Forum-Movies',
        'tv': '/Forum-Tv-Shows-FULL-SERIES',
    }

    # MyBB-specific selectors
    THREAD_SELECTORS = {
        'thread_row': 'tr.inline_row',
        'thread_link': 'a[id^="tid_"]',
        'thread_title': 'span.subject_new, span.subject_old',
        'pagination': 'div.pagination a',
    }

    POST_SELECTORS = {
        'post_content': 'div.post_body',
        'hidden_content': 'div.hidden_content, div.mybb_hidden, .hiddenContent',
        'reply_form': 'form#quick_reply_form, form[action*="newreply"]',
        'reply_textarea': 'textarea#message, textarea[name="message"]',
        'submit_button': 'input[name="submit"], button[type="submit"]',
    }

    # File host patterns
    HOST_PATTERNS = {
        'rapidgator': re.compile(
            r'https?://(?:www\.)?rapidgator\.net/file/[a-zA-Z0-9]+(?:/[^\s<>"\']*)?',
            re.IGNORECASE
        ),
        'nitroflare': re.compile(
            r'https?://(?:www\.)?nitroflare\.com/view/[a-zA-Z0-9]+(?:/[^\s<>"\']*)?',
            re.IGNORECASE
        ),
        '1fichier': re.compile(
            r'https?://(?:www\.)?1fichier\.com/\?[a-zA-Z0-9]+',
            re.IGNORECASE
        ),
        'ddownload': re.compile(
            r'https?://(?:www\.)?ddownload\.com/[a-zA-Z0-9]+(?:/[^\s<>"\']*)?',
            re.IGNORECASE
        ),
    }

    @classmethod
    def get_config(cls) -> SourceConfig:
        """Return Adit-HD configuration."""
        return SourceConfig(
            name="adithd",
            display_name="Adit-HD",
            base_url=cls.BASE_URL,
            capabilities=(
                SourceCapability.MOVIES |
                SourceCapability.TV_SHOWS |
                SourceCapability.PAGINATION |
                SourceCapability.SEARCH |
                SourceCapability.DIRECT_LINKS
            ),
            rate_limit=3.0,  # Conservative rate limiting for forum
            requires_auth=True,
            requires_cloudflare_bypass=False,
            timeout=30,
            priority=85,
            enabled=True
        )

    @staticmethod
    def _replied_threads_path() -> str:
        if os.name == 'nt':
            base = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
            data_dir = os.path.join(base, 'ScanHound')
        else:
            data_dir = os.path.join(os.path.expanduser('~'), '.local', 'share', 'scanhound')
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, 'adithd_replied_threads.json')

    def __init__(self):
        """Initialize Adit-HD source."""
        super().__init__()
        self._scraper = None
        self._session_cookies = None
        self._is_logged_in = False
        self._driver = None
        self._credentials = None
        self._replied_threads = self._load_replied_threads()

    def _load_replied_threads(self) -> set:
        """Load previously replied thread IDs from disk."""
        try:
            path = self._replied_threads_path()
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return set(json.load(f))
        except Exception as e:
            logger.debug("Could not load replied threads: %s", e)
        return set()

    def _save_replied_threads(self):
        """Persist replied thread IDs to disk."""
        try:
            path = self._replied_threads_path()
            with open(path, 'w') as f:
                json.dump(list(self._replied_threads), f)
        except Exception as e:
            logger.debug("Could not save replied threads: %s", e)

    def set_credentials(self, username: str, password: str, auto_reply: bool = False):
        """Set login credentials for authenticated access.

        Args:
            username: Forum username
            password: Forum password
            auto_reply: Enable auto-reply for hidden content
        """
        self._credentials = {
            'username': username,
            'password': password,
            'auto_reply': auto_reply
        }

    def set_driver(self, driver):
        """Inject Selenium WebDriver for authentication and scraping.

        Args:
            driver: Selenium WebDriver instance
        """
        self._driver = driver

    def _get_scraper(self):
        """Get or create cloudscraper/requests instance."""
        if self._scraper is None:
            try:
                import cloudscraper
                self._scraper = cloudscraper.create_scraper(
                    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
                )
            except ImportError:
                import requests
                self._scraper = requests.Session()

            # Apply session cookies if logged in
            if self._session_cookies:
                for cookie in self._session_cookies:
                    self._scraper.cookies.set(cookie['name'], cookie['value'])

        return self._scraper

    async def login(self) -> bool:
        """Login to Adit-HD forum using Selenium.

        Returns:
            bool: True if login successful
        """
        if not self._driver or not self._credentials:
            logger.warning("[Adit-HD] Cannot login: missing driver or credentials")
            return False

        if self._is_logged_in:
            return True

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            # Navigate to login page
            login_url = f"{self.LOGIN_URL}?action=login"
            self._driver.get(login_url)

            wait = WebDriverWait(self._driver, 10)

            # Find and fill username field
            username_field = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//input[@name='username' or @id='username']")
            ))
            username_field.clear()
            username_field.send_keys(self._credentials['username'])

            # Find and fill password field
            password_field = self._driver.find_element(
                By.XPATH, "//input[@name='password' or @id='password' or @type='password']"
            )
            password_field.clear()
            password_field.send_keys(self._credentials['password'])

            # Find and click login button
            login_btn = self._driver.find_element(
                By.XPATH,
                "//input[@type='submit' and contains(@value, 'Login')] | "
                "//input[@type='submit' and @name='submit'] | "
                "//button[contains(text(), 'Login')]"
            )
            login_btn.click()

            # Wait for login to complete
            await asyncio.sleep(2)

            # Verify login by checking for logout link or user panel
            try:
                wait.until(EC.presence_of_element_located(
                    (By.XPATH,
                     "//a[contains(@href, 'logout') or contains(text(), 'Logout') or "
                     "contains(text(), 'Log Out')]")
                ))
                self._is_logged_in = True

                # Store cookies for cloudscraper
                self._session_cookies = self._driver.get_cookies()

                logger.info("[Adit-HD] Login successful")
                return True

            except Exception as e:
                logger.warning("[Adit-HD] Login may have failed - no logout link found: %s", e)
                return False

        except Exception as e:
            logger.error("[Adit-HD] Login error: %s", e)
            return False

    async def fetch_page(
        self,
        page: int = 1,
        mode: str = "movies",
        **kwargs
    ) -> PageResult:
        """Fetch a page of thread listings from Adit-HD forum.

        Args:
            page: Page number (1-indexed)
            mode: "movies", "tv", or "all"

        Returns:
            PageResult with releases
        """
        releases = []
        errors = []
        has_next = False

        # Ensure login if credentials provided
        if self._credentials and not self._is_logged_in:
            await self.login()

        # Determine URL based on mode
        if mode == "movies":
            url_path = self.URL_PATTERNS['movies']
        elif mode == "tv":
            url_path = self.URL_PATTERNS['tv']
        else:
            # "all" mode - fetch both
            movies_result = await self.fetch_page(page, "movies")
            tv_result = await self.fetch_page(page, "tv")
            return PageResult(
                releases=movies_result.releases + tv_result.releases,
                has_next=movies_result.has_next or tv_result.has_next,
                errors=movies_result.errors + tv_result.errors
            )

        # Build URL with pagination
        if page > 1:
            url = f"{self.BASE_URL}{url_path}?page={page}"
        else:
            url = f"{self.BASE_URL}{url_path}"

        try:
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, url)

            if not html:
                errors.append(f"Failed to fetch {url}")
                return PageResult(releases=[], errors=errors)

            # Parse thread listings
            soup = BeautifulSoup(html, 'html.parser')
            threads = soup.select(self.THREAD_SELECTORS['thread_row'])

            for thread in threads:
                try:
                    release = self._parse_thread_row(thread, mode)
                    if release:
                        releases.append(release)
                except Exception as e:
                    logger.debug("Failed to parse thread: %s", e)
                    continue

            # Check for next page
            has_next = self._has_next_page(soup)

            logger.debug("[Adit-HD] Fetched %s releases from %s", len(releases), url)

        except Exception as e:
            logger.error("[Adit-HD] Error fetching page: %s", e)
            errors.append(str(e))

        return PageResult(
            releases=releases,
            current_page=page,
            has_next=has_next,
            errors=errors
        )

    def _fetch_html(self, url: str) -> Optional[str]:
        """Fetch HTML from URL, preferring Selenium if logged in."""
        # If we have a logged-in driver, use it
        if self._is_logged_in and self._driver:
            try:
                self._driver.get(url)
                time.sleep(1)  # Wait for page load
                return self._driver.page_source
            except Exception as e:
                logger.debug("[Adit-HD] Driver fetch failed, falling back: %s", e)

        # Fallback to cloudscraper with cookies (retry-aware via base class)
        return super()._fetch_html(url)

    def _parse_thread_row(self, row: Any, mode: str) -> Optional[ParsedRelease]:
        """Parse a thread row from the forum listing."""
        try:
            # Find thread link - try multiple selectors
            link = row.select_one(self.THREAD_SELECTORS['thread_link'])
            if not link:
                link = row.select_one('a[href*="Thread-"]')
            if not link:
                link = row.select_one('span.subject_new a, span.subject_old a')
            if not link:
                # Last resort - any link in the row that looks like a thread
                for a in row.select('a'):
                    href = a.get('href', '')
                    if 'Thread-' in href or 'showthread' in href:
                        link = a
                        break

            if not link:
                return None

            title = link.get_text(strip=True)
            url = link.get('href', '')

            # Ensure absolute URL
            if url and not url.startswith('http'):
                url = f"{self.BASE_URL}/{url.lstrip('/')}"

            if not title or not url:
                return None

            return self.parse_release({
                'title': title,
                'url': url,
                'mode': mode,
                'row_html': str(row)
            })

        except Exception as e:
            logger.debug("[Adit-HD] Error parsing thread row: %s", e)
            return None

    def parse_release(self, raw_data: Any) -> Optional[ParsedRelease]:
        """Parse raw thread data into a ParsedRelease."""
        if not isinstance(raw_data, dict):
            return None

        title = raw_data.get('title', '')
        url = raw_data.get('url', '')
        mode = raw_data.get('mode', 'movies')

        if not title:
            return None

        # Extract metadata from title
        year = self.extract_year(title)
        resolution = self.extract_resolution(title)
        season, episode = self.extract_season_episode(title)
        is_hdr, is_dovi, hdr_format = self.extract_hdr_info(title)
        codec = self.extract_codec(title)
        audio = self.extract_audio_codec(title)

        # Determine if TV
        is_tv = mode == 'tv' or self.is_tv_release(title)

        # Clean title for display
        display_title = self._extract_display_title(title, year)

        # Check for season pack vs single episode
        is_season_pack = season is not None and episode is None

        # Detect remux/web
        is_remux = 'remux' in title.lower()
        is_web = any(x in title.lower() for x in ['web-dl', 'webdl', 'webrip', 'amzn', 'nf', 'atvp'])

        # Extract release group
        release_group = self._extract_release_group(title)

        return ParsedRelease(
            title=title,
            url=url,
            source="adithd",
            display_title=display_title,
            year=year,
            resolution=resolution,
            is_hdr=is_hdr,
            is_dovi=is_dovi,
            hdr_format=hdr_format,
            codec=codec,
            audio_codec=audio,
            is_remux=is_remux,
            is_web=is_web,
            release_group=release_group,
            is_tv=is_tv,
            season=season,
            episode=episode,
            is_season_pack=is_season_pack,
            raw_data=raw_data
        )

    def _extract_display_title(self, title: str, year: int) -> str:
        """Extract clean display title."""
        # Remove MULTI tag if present
        title = re.sub(r'^\[?MULTI\]?\s*', '', title, flags=re.IGNORECASE)

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

        # Clean up separators
        title = re.sub(r'[._-]', ' ', title)
        title = re.sub(r'\s+', ' ', title)

        return title.strip()

    def _extract_release_group(self, title: str) -> str:
        """Extract release group from title."""
        return super()._extract_release_group(title)

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        """Check if there's a next page in MyBB pagination."""
        # Look for "Next" link in pagination
        pagination = soup.select('div.pagination a, span.pagination a, .pagination_page')
        for link in pagination:
            text = link.get_text(strip=True).lower()
            if 'next' in text or text == '>' or text == '>>':
                return True

        # Alternative: check page numbers
        current = soup.select_one('span.pagination_current, .pagination_current')
        if current:
            try:
                current_num = int(current.get_text(strip=True))
                for page_link in pagination:
                    try:
                        num = int(page_link.get_text(strip=True))
                        if num > current_num:
                            return True
                    except ValueError:
                        continue
            except ValueError:
                pass

        return False

    async def fetch_thread_content(self, url: str) -> Tuple[str, List[str]]:
        """Fetch thread content and extract download links.

        Handles hidden content that requires login or reply.

        Args:
            url: Thread URL

        Returns:
            Tuple of (page_content, list_of_links)
        """
        links = []

        try:
            # Use Selenium if logged in for hidden content
            if self._is_logged_in and self._driver:
                self._driver.get(url)
                await asyncio.sleep(2)  # Wait for page load

                # Check for hidden content that needs reply
                if self._credentials and self._credentials.get('auto_reply'):
                    await self._handle_hidden_content(url)

                html = self._driver.page_source
            else:
                loop = asyncio.get_running_loop()
                html = await loop.run_in_executor(None, self._fetch_html, url)

            if html:
                links = self._extract_download_links(html)

            return html or '', links

        except Exception as e:
            logger.error("[Adit-HD] Error fetching thread content: %s", e)
            return '', []

    async def _handle_hidden_content(self, url: str):
        """Handle reply-required hidden content sections."""
        # Don't reply twice to the same thread
        if url in self._replied_threads:
            return

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            soup = BeautifulSoup(self._driver.page_source, 'html.parser')

            # Check for hidden content indicators
            hidden_elements = soup.select(self.POST_SELECTORS['hidden_content'])
            if not hidden_elements:
                return  # No hidden content

            # Check if content is already visible (we're logged in and have replied before)
            visible_links = self._extract_download_links(str(soup))
            if visible_links:
                return  # Links already visible

            # Check if reply form exists
            reply_form = soup.select_one(self.POST_SELECTORS['reply_form'])
            if not reply_form:
                return  # No reply form available

            logger.info("[Adit-HD] Hidden content detected, attempting auto-reply...")

            # Find and fill reply textarea
            wait = WebDriverWait(self._driver, 10)
            textarea = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//textarea[@id='message' or @name='message']")
            ))
            textarea.clear()
            textarea.send_keys("Thanks for sharing!")

            # Submit reply
            submit_btn = self._driver.find_element(
                By.XPATH,
                "//input[@name='submit' and @type='submit'] | "
                "//button[contains(text(), 'Post')] | "
                "//input[@type='submit' and contains(@value, 'Post')]"
            )
            self._driver.execute_script("arguments[0].scrollIntoView();", submit_btn)
            await asyncio.sleep(0.3)
            submit_btn.click()

            # Wait for page reload
            await asyncio.sleep(3)

            # Mark this thread as replied and persist
            self._replied_threads.add(url)
            self._save_replied_threads()

            logger.info("[Adit-HD] Auto-replied to reveal hidden content")

        except Exception as e:
            logger.debug("[Adit-HD] Could not handle hidden content: %s", e)

    def _extract_download_links(
        self,
        html: str,
        preferred_host: Optional[str] = None
    ) -> List[str]:
        """Extract download links from thread content.

        Args:
            html: Page HTML content
            preferred_host: Preferred file host (rapidgator, nitroflare, 1fichier)

        Returns:
            List of download links
        """
        links: Dict[str, List[str]] = {host: [] for host in self.HOST_PATTERNS.keys()}

        for host, pattern in self.HOST_PATTERNS.items():
            found = pattern.findall(html)
            links[host].extend(found)

        # Return preferred host links if available (order-preserving dedup so
        # multi-part archives keep their sequence and the host-priority order
        # below is not discarded by set() randomization).
        if preferred_host and links.get(preferred_host):
            return list(dict.fromkeys(links[preferred_host]))

        # Otherwise return all links, prioritized by common preference
        all_links = []
        priority_order = ['rapidgator', 'nitroflare', '1fichier', 'ddownload']
        for host in priority_order:
            if host in links:
                all_links.extend(links[host])

        return list(dict.fromkeys(all_links))

    async def search(
        self,
        query: str,
        mode: str = "all",
        **kwargs
    ) -> PageResult:
        """Search Adit-HD forum."""
        # MyBB requires action=do_search to perform the search;
        # action=results only retrieves results from a prior session search.
        search_url = f"{self.BASE_URL}/search.php?action=do_search&keywords={query.replace(' ', '+')}&postthread=1&sortby=dateline&sortordr=desc"

        try:
            loop = asyncio.get_running_loop()
            html = await loop.run_in_executor(None, self._fetch_html, search_url)

            if not html:
                return PageResult(releases=[], errors=["Search failed"])

            soup = BeautifulSoup(html, 'html.parser')
            threads = soup.select(self.THREAD_SELECTORS['thread_row'])

            releases = []
            for thread in threads:
                try:
                    release = self._parse_thread_row(thread, mode)
                    if release:
                        # Filter by mode if specified
                        if mode == "movies" and release.is_tv:
                            continue
                        if mode == "tv" and not release.is_tv:
                            continue
                        releases.append(release)
                except Exception as e:
                    logger.debug("[Adit-HD] Failed to parse search result: %s", e)
                    continue

            return PageResult(
                releases=releases,
                has_next=self._has_next_page(soup)
            )

        except Exception as e:
            logger.error("[Adit-HD] Search error: %s", e)
            return PageResult(releases=[], errors=[str(e)])

    async def fetch_download_links(
        self,
        release: ParsedRelease,
        service: str = "rapidgator"
    ) -> List[str]:
        """Fetch download links for a release.

        Args:
            release: The release to get links for
            service: Preferred hosting service

        Returns:
            List of download URLs
        """
        try:
            _, links = await self.fetch_thread_content(release.url)

            # Filter by service if specified
            if service:
                service_lower = service.lower()
                pattern = self.HOST_PATTERNS.get(service_lower)
                if pattern:
                    links = [link for link in links if pattern.match(link)]

            return links

        except Exception as e:
            logger.error("[Adit-HD] Error fetching download links: %s", e)
            return []
