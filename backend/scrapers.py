"""Scrapers — Web scraping facade.

Provides the WebScrapers class, which composes four specialized scraping
sub-modules into a single object for backward-compatible use by callers:

    rt_scraper.py      RTScraper       — Rotten Tomatoes critics/audience scores
    imdb_scraper.py    IMDbScraper     — IMDb rating fallback
    detail_scraper.py  DetailScraper   — HDEncode post detail parsing
    link_scraper.py    LinkScraper     — Selenium download-link resolution

All callers that previously imported WebScrapers continue to work unchanged.
"""

from backend.rt_scraper import RTScraper
from backend.imdb_scraper import IMDbScraper
from backend.detail_scraper import DetailScraper
from backend.link_scraper import LinkScraper


class WebScrapers:
    """Facade composing the four specialized scraping sub-modules.

    Instantiate once per scan session and pass to ScannerService.  All
    public methods delegate to the appropriate sub-module while keeping
    the same signature as before the refactor.
    """

    def __init__(self, parent_app):
        """Initialize all sub-scraper instances.

        Args:
            parent_app: AppService instance (provides config, caches,
                        parse_size, clean_string, safe_log).
        """
        self.app = parent_app
        self._rt = RTScraper(parent_app)
        self._imdb = IMDbScraper(parent_app)
        self._detail = DetailScraper(parent_app)
        self._links = LinkScraper(parent_app)

    # ── Rotten Tomatoes ────────────────────────────────────────────────

    def scrape_rt_score(self, title, year=None, is_tv=False):
        """Scrape Rotten Tomatoes critics and audience scores.

        See ``RTScraper.scrape_rt_score`` for full documentation.
        """
        return self._rt.scrape_rt_score(title, year=year, is_tv=is_tv)

    def _title_to_rt_slug(self, title):
        """Convert a title string to an RT URL slug. Delegates to RTScraper."""
        return self._rt._title_to_rt_slug(title)

    def _scrape_rt_tv_direct(self, title, year=None):
        """Scrape RT TV score by direct URL. Delegates to RTScraper."""
        return self._rt._scrape_rt_tv_direct(title, year=year)

    def _scrape_rt_movie_direct(self, title, year=None):
        """Scrape RT movie score by direct URL. Delegates to RTScraper."""
        return self._rt._scrape_rt_movie_direct(title, year=year)

    # ── IMDb ───────────────────────────────────────────────────────────

    def scrape_imdb_data(self, imdb_id):
        """Scrape IMDb rating and vote count as OMDb fallback.

        See ``IMDbScraper.scrape_imdb_data`` for full documentation.
        """
        return self._imdb.scrape_imdb_data(imdb_id)

    # ── HDEncode detail ────────────────────────────────────────────────

    def scrape_details(self, url, headers, scraper=None):
        """Scrape media metadata from an HDEncode post page.

        See ``DetailScraper.scrape_details`` for full documentation.
        """
        return self._detail.scrape_details(url, headers, scraper=scraper)

    # ── Selenium link resolution ───────────────────────────────────────

    def scrape_links_with_driver(self, driver, url, service_type):
        """Scrape Rapidgator/Nitroflare links using Selenium.

        See ``LinkScraper.scrape_links_with_driver`` for full documentation.
        """
        return self._links.scrape_links_with_driver(driver, url, service_type)

    def resolve_cuty_link(self, driver, cuty_url, credentials=None):
        """Resolve a cuty.io/cuttlinks.com shortlink to its final URL.

        See ``LinkScraper.resolve_cuty_link`` for full documentation.
        """
        return self._links.resolve_cuty_link(driver, cuty_url, credentials=credentials)

    def resolve_cuty_links_batch(self, driver, cuty_urls, credentials=None):
        """Resolve a batch of shortlinks with browser fallback.

        See ``LinkScraper.resolve_cuty_links_batch`` for full documentation.
        """
        return self._links.resolve_cuty_links_batch(driver, cuty_urls, credentials=credentials)

    def open_cuty_in_browser(self, cuty_url):
        """Open a shortlink in the system's default browser as a fallback.

        See ``LinkScraper.open_cuty_in_browser`` for full documentation.
        """
        return self._links.open_cuty_in_browser(cuty_url)
