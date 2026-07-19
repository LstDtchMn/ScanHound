"""ScannerService — Web scraping scan engine and media item models.

Framework-agnostic: communicates via callbacks, no UI dependencies.
"""

import asyncio
import json
import logging
import re
import time
import threading
import requests
import cloudscraper
from bs4 import BeautifulSoup
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from backend.app_service import (
    LRUCache, clean_string, normalize_title, STATUS_MISSING,
)
from backend.database import DatabaseManager
from backend.matching import MatchingEngine, clear_fuzzy_cache
from backend.metadata_enricher import MetadataEnricher
from backend.scrapers import WebScrapers

logger = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────

class ScanStatus(Enum):
    MISSING = "missing"
    MISSING_SEASON = "missing_season"
    DOWNLOADED = "downloaded"
    # A sibling release of a title you've already grabbed, that is NOT a quality
    # upgrade over the grab (same-or-worse) — you effectively have it already.
    DOWNLOADED_SIMILAR = "downloaded_similar"
    IN_LIBRARY = "in_library"
    UPGRADE = "upgrade"
    DV_UPGRADE = "dv_upgrade"


@dataclass
class MediaItem:
    """Represents a single media item in scan results."""
    id: str
    title: str
    year: int
    season: Optional[int] = None
    episodes: Optional[int] = None
    rating: float = 0.0
    votes: int = 0
    votes_source: str = ""
    rt_score: Optional[int] = None
    status: ScanStatus = ScanStatus.MISSING
    status_text: str = "Missing"
    color: str = "#e74c3c"
    resolution: str = ""
    size: str = ""
    hdr: str = ""
    dovi: bool = False
    genres: List[str] = field(default_factory=list)
    language: str = ""
    url: str = ""
    plex_info: str = "-"
    plex_versions: str = "[]"
    plex_rating_key: Optional[str] = None
    selected: bool = False
    host_pref: str = "RG"
    poster_path: Optional[str] = None
    imdb_id: Optional[str] = None
    tile_state: int = 0
    description: str = ""
    posted_date: Optional[str] = None
    web_data: Dict[str, Any] = field(default_factory=dict)
    group_key: str = ""
    is_duplicate_group: bool = False
    prior_grab: Optional[Dict[str, Any]] = None
    # Crawl category this item came from: '4k' | 'remux' | 'tv' | '' (unknown).
    # Drives the instant 4K/Remux/TV display filter in the UI.
    category: str = ""


@dataclass
class WatchlistItem:
    """Represents an item in the watchlist."""
    tmdb_id: int
    media_type: str
    title: str
    year: int
    poster_path: Optional[str] = None
    overview: str = ""
    rating: float = 0.0
    language: str = ""
    genres: List[str] = field(default_factory=list)
    added_date: str = ""
    priority: int = 0
    notes: str = ""
    in_plex: bool = False
    web_data: Dict[str, Any] = field(default_factory=dict)
    group_key: str = ""
    is_duplicate_group: bool = False


# ── Status constants ──────────────────────────────────────────────────

STATUS_COLORS = {
    ScanStatus.MISSING: "#e74c3c",
    ScanStatus.MISSING_SEASON: "#d35400",
    ScanStatus.DOWNLOADED: "#17a2b8",
    ScanStatus.DOWNLOADED_SIMILAR: "#f97316",
    ScanStatus.IN_LIBRARY: "#27ae60",
    ScanStatus.UPGRADE: "#f39c12",
    ScanStatus.DV_UPGRADE: "#9b59b6",
}

STATUS_TEXTS = {
    ScanStatus.MISSING: "Missing",
    ScanStatus.MISSING_SEASON: "Missing Season!",
    ScanStatus.DOWNLOADED: "Downloaded",
    ScanStatus.DOWNLOADED_SIMILAR: "Downloaded Similar",
    ScanStatus.IN_LIBRARY: "\u2713 In Library",
    ScanStatus.UPGRADE: "UPGRADE",
    ScanStatus.DV_UPGRADE: "UPGRADE (DV)",
}

# Resolution ranking for the "is this sibling an upgrade over what I grabbed?"
# check used by ``_download_status_for``.
_RES_RANK = {"2160p": 4, "4k": 4, "uhd": 4, "1080p": 3, "720p": 2, "480p": 1}


def _res_rank(res) -> int:
    return _RES_RANK.get((res or "").strip().lower(), 0)


# ── ScannerService ────────────────────────────────────────────────────

class ScannerService:
    """Orchestrates web scraping scans and Plex matching."""

    def __init__(
        self,
        config: Dict[str, Any],
        db: DatabaseManager,
        scrapers: WebScrapers,
        matching: MatchingEngine,
        plex_service,  # PlexService (avoid circular import)
        tmdb_cache: Optional[LRUCache] = None,
        omdb_cache: Optional[LRUCache] = None,
    ):
        self.config = config
        self.db = db
        self.scrapers = scrapers
        self.matching = matching
        self.plex = plex_service
        self.tmdb_cache = tmdb_cache or LRUCache(2000)
        self.omdb_cache = omdb_cache or LRUCache(2000)
        self._enricher = MetadataEnricher(config, scrapers, self.omdb_cache)

        # Scan state
        self.items: List[MediaItem] = []
        self.filtered_items: List[MediaItem] = []
        self.grouped_items: Dict[str, List[MediaItem]] = {}
        self.expanded_groups: Set[str] = set()
        self._items_lock = threading.Lock()
        self._item_counter = 0
        self._stop_event = threading.Event()
        self._scanning_lock = threading.Lock()
        self._is_scanning = False
        # Single global scan slot. Foreground (manual/scheduled) and background
        # pre-cache scans share this one ScannerService instance, so they must
        # never execute concurrently — both paths claim this slot first.
        self._scan_slot = threading.Lock()
        # URLs seen in the most recent listing crawl (new + skipped), exposed so
        # the background scanner can refresh last_seen on still-listed items.
        self._last_crawl_seen_urls: Set[str] = set()
        # True when the last crawl stopped early at cached content — the scanner
        # then never saw deeper pages, so it must NOT purge against this crawl.
        self._last_crawl_early_stopped: bool = False

        # Download history
        self.download_history: Set[str] = set()
        self._downloaded_titles_lookup: Dict[str, List[Dict]] = {}

        # Callbacks
        self._log_fn: Optional[Callable[[str, str], None]] = None
        self._progress_fn: Optional[Callable[[float, str], None]] = None

    # ── Thread-safe scan state ───────────────────────────────────────

    @property
    def stop_scan_flag(self) -> bool:
        return self._stop_event.is_set()

    @stop_scan_flag.setter
    def stop_scan_flag(self, value: bool):
        if value:
            self._stop_event.set()
        else:
            self._stop_event.clear()

    @property
    def is_scanning(self) -> bool:
        with self._scanning_lock:
            return self._is_scanning

    @is_scanning.setter
    def is_scanning(self, value: bool):
        with self._scanning_lock:
            self._is_scanning = value

    # ── Global scan slot (foreground vs background mutual exclusion) ──

    def try_acquire_scan(self) -> bool:
        """Atomically claim the single scan slot. Returns False if a scan
        (foreground or background) is already running. The caller must call
        ``release_scan()`` in a finally block when its scan completes."""
        return self._scan_slot.acquire(blocking=False)

    def release_scan(self) -> None:
        """Release the scan slot. Safe to call even if not held."""
        try:
            self._scan_slot.release()
        except RuntimeError:
            pass

    @property
    def scan_in_progress(self) -> bool:
        """True if any scan currently holds the slot (best-effort, for friendly
        409s — the authoritative guard is ``try_acquire_scan``)."""
        return self._scan_slot.locked()

    # ── Callbacks ─────────────────────────────────────────────────────

    def set_log_callback(self, fn: Callable[[str, str], None]):
        """Register a function to receive log messages (msg, level)."""
        self._log_fn = fn

    def set_progress_callback(self, fn: Callable[[float, str], None]):
        """Register a function to receive progress updates (0.0–1.0, text)."""
        self._progress_fn = fn

    def _log(self, msg: str, level: str = "info"):
        """Emit a log message to both Python logging and the UI callback."""
        getattr(logger, level if level != "success" else "info", logger.info)(msg)
        if self._log_fn:
            try:
                self._log_fn(msg, level)
            except Exception:
                pass

    def _progress(self, value: float, text: str):
        """Push a progress update (0.0–1.0) to the UI callback."""
        if self._progress_fn:
            try:
                self._progress_fn(value, text)
            except Exception:
                pass

    # ── Scan execution ────────────────────────────────────────────────

    def run_scan(
        self,
        scan_type: str,
        source_type: str,
        pages: int = 1,
        resolution_flags: Optional[Dict[str, bool]] = None,
        search_query: str = "",
        use_expired_cache: bool = False,
        plex_refresh_mode: str = "auto",
        track_urls: bool = True,
        skip_urls: Optional[Set[str]] = None,
        early_stop: bool = False,
    ) -> List[MediaItem]:
        """Run a full scan synchronously (call from background thread).

        Args:
            scan_type: "Incremental", "Deep Scan", "Loaded Scan", "Site Search"
            source_type: "HDEncode", "DDLBase", "Adit-HD"
            pages: Number of pages to crawl per source
            resolution_flags: {"4k": True, "1080p": True, "remux": False, "tv": True}
            search_query: Query string for Site Search mode
            use_expired_cache: If True, skip cache validation and use cached data as-is
            plex_refresh_mode: "auto" (smart), "force_refresh" (always reload), "cache_only" (never reload)
            track_urls: If False, leave the incremental ``scanned_urls`` table
                untouched (the background pre-cache uses this so it doesn't reset
                the baseline the scheduled incremental scans rely on).
            skip_urls: Extra URLs to skip detail-processing (e.g. URLs the
                background scanner already has cached), merged with whatever the
                scan type loads.
            early_stop: If True, stop crawling a source once a populated listing
                page yields no new (non-skipped) posts — the "previous endpoint".

        Returns:
            List of MediaItem results.
        """
        pages = min(max(1, pages), 99)
        flags = resolution_flags or {"4k": True, "1080p": False, "remux": False, "tv": False}

        self.stop_scan_flag = False
        self.is_scanning = True
        with self._items_lock:
            self.items.clear()
            self._item_counter = 0
        self._last_crawl_seen_urls = set()

        # Load download history
        self.download_history = self._load_download_history()
        # Sync to matching engine's app bridge so check_download_history() works
        self.matching.app.download_history = self.download_history

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    self._run_scan_async(scan_type, source_type, pages, flags,
                                        search_query, use_expired_cache, plex_refresh_mode,
                                        track_urls, skip_urls, early_stop)
                )
            finally:
                loop.close()
        except Exception as e:
            self._log(f"Scan error: {e}", "error")
        finally:
            self.is_scanning = False

        return list(self.items)

    async def _run_scan_async(
        self,
        scan_type: str,
        source_type: str,
        pages: int,
        flags: Dict[str, bool],
        search_query: str,
        use_expired_cache: bool = False,
        plex_refresh_mode: str = "auto",
        track_urls: bool = True,
        skip_urls: Optional[Set[str]] = None,
        early_stop: bool = False,
    ):
        """Internal async scan implementation."""

        # Clear stale fuzzy-match cache from previous scans so Plex title
        # updates are picked up rather than served from the LRU cache.
        clear_fuzzy_cache()

        # ── Validate cache for incremental/loaded scans ───────────────
        force_plex_reload = False
        if scan_type in ("Incremental", "Loaded Scan") and not use_expired_cache:
            is_valid, msg = self.plex.check_cache_status()
            if not is_valid:
                self._log(msg, "warning")
                if scan_type == "Loaded Scan":
                    self._log("Falling back to Deep Scan", "warning")
                    scan_type = "Deep Scan"
                else:
                    self._log("Refreshing Plex cache, scan remains incremental")
                    force_plex_reload = True
        elif use_expired_cache:
            self._log("Using expired cache per user choice")

        # ── Load Plex libraries if needed ─────────────────────────────
        # plex_refresh_mode can override automatic behavior:
        # - "auto": Use smart logic (5-minute cooldown) [default]
        # - "force_refresh": Always reload from Plex API
        # - "cache_only": Never reload from Plex API this session

        force_reload = (scan_type == "Deep Scan") or force_plex_reload
        use_cache = scan_type in ("Incremental", "Loaded Scan") and not force_plex_reload

        if plex_refresh_mode == "cache_only":
            # Never reload from Plex API
            force_reload = False
            use_cache = True
            self._log("Cache-only mode: using Plex cache without reload")
        elif plex_refresh_mode == "force_refresh":
            # Always reload from Plex API
            force_reload = True
            use_cache = False
            self._log("Force-refresh mode: reloading from Plex API")
        elif plex_refresh_mode == "auto":
            # Skip redundant Deep Scan reload when Plex data is already in memory
            # and was loaded within the last 5 minutes (e.g. back-to-back scans).
            # force_plex_reload (cache-expired path) always reloads regardless.
            if force_reload and not force_plex_reload and self.plex.plex_movies:
                age_s = time.time() - getattr(self.plex, '_last_full_load_time', 0)
                if age_s < 300:
                    force_reload = False
                    use_cache = True
                    self._log(f"Plex data is fresh ({age_s:.0f}s old), skipping reload")

        if self.plex._plex_loading:
            self._log("Waiting for Plex library load to finish...")
            self._progress(0.05, "Waiting for Plex...")
            self.plex.load_libraries(wait_if_loading=True, progress_callback=self._progress, use_cache=use_cache)
            self._log("Plex library load complete")
        elif force_reload or not self.plex.plex_movies:
            self._log("Loading Plex libraries...")
            self._progress(0.05, "Loading Plex libraries...")
            self.plex.load_libraries(progress_callback=self._progress, use_cache=use_cache)

        # Warn loudly if the Plex library is empty — otherwise every item is
        # matched against nothing and reported as Missing (misleading results).
        if not self.plex.plex_movies and not self.plex.plex_tv:
            self._log(
                "⚠ Plex library is empty / not connected — every item will show as "
                "Missing. Check the Plex connection in Settings before trusting these results.",
                "warning",
            )

        # ── Build source URLs ─────────────────────────────────────────
        base_url = self.config.get("base_url", "https://hdencode.org").rstrip('/')
        sources = self._build_sources(scan_type, source_type, base_url, flags, search_query)

        if not sources:
            if ((scan_type == "Site Search" or source_type == "HDEncode")
                    and not self.config.get("hdencode_enabled", True)):
                self._log("HDEncode is disabled in Settings; no requests were made.", "warning")
            else:
                self._log("No sources selected!", "error")
            return

        self._log(f"Sources: {', '.join(s['name'] for s in sources)}")

        # ── Crawl pages ───────────────────────────────────────────────
        scraper = cloudscraper.create_scraper()
        loop = asyncio.get_running_loop()

        previously_scanned: Set[str] = set()
        if scan_type == "Deep Scan":
            if track_urls:
                self.db.clear_scanned_urls()
                self._log("Deep Scan: Cleared URL history, scanning all items")
            else:
                self._log("Deep Scan: scanning all items (incremental URL history left untouched)")
        elif scan_type == "Site Search":
            # Ad hoc searches should always show current matches and should not
            # contaminate the incremental URL history used by scheduled scans.
            self._log("Site Search: ignoring incremental URL history")
        else:
            # Load previously scanned URLs so incremental scans skip them
            previously_scanned = self.db.get_scanned_urls()
            if previously_scanned:
                self._log(f"Incremental: {len(previously_scanned)} previously scanned URLs loaded")

        # A caller-supplied skip set (e.g. the background pre-cache passes URLs it
        # already has cached) augments whatever the scan type loaded.
        if skip_urls:
            previously_scanned = previously_scanned | set(skip_urls)
            self._log(f"Skipping {len(skip_urls)} already-cached URL(s)")

        all_posts = await self._crawl_pages(
            sources, pages, base_url, scraper, loop, previously_scanned, early_stop)

        if self.stop_scan_flag:
            return

        self._log(f"Found {len(all_posts)} posts, processing details...")

        # ── Process posts (parallel) ──────────────────────────────────
        num_threads = self.config.get("scan_threads", 10)
        await self._process_posts(all_posts, scraper, num_threads)

        if self.stop_scan_flag:
            return

        # Save scanned URLs only after all posts are processed — avoids
        # permanently marking unvisited URLs as "seen" if the scan is stopped.
        if track_urls and all_posts and scan_type != "Site Search":
            urls_to_save = [{'url': p['url'], 'title': None, 'source': p.get('source')} for p in all_posts]
            self.db.add_scanned_urls_batch(urls_to_save)

        # ── Sort by posted date (newest first) ────────────────────────
        self.items.sort(key=self._posted_date_sort_key, reverse=True)

        # ── Match against Plex ────────────────────────────────────────
        self._log("Matching against Plex library...")
        await self._match_against_plex(scan_type)

        # ── Mark missing seasons from downloaded siblings ────────────
        self._mark_missing_seasons_from_scan()

        # ── Enrich metadata ───────────────────────────────────────────
        await self._enrich_metadata_async()

        # ── Finalize grouping ─────────────────────────────────────────
        # group_key was seeded at parse time, but enrichment can CORRECT a
        # garbage-parsed title (e.g. a release-group leak "gua-killingfaith" ->
        # "Killing Faith") and fill in the year. Rebuild it now from the final
        # canonical title/year/season so a fixed title groups (and dedups) under
        # the right key instead of a frozen bogus one.
        self._assign_group_keys()

        self._log(f"Scan complete: {len(self.items)} items found", "success")

    # ── Source building ───────────────────────────────────────────────

    def _build_sources(
        self, scan_type: str, source_type: str, base_url: str,
        flags: Dict[str, bool], search_query: str,
    ) -> List[Dict]:
        """Build the list of source descriptors to crawl for this scan.

        Each descriptor is a dict with keys:
            name   – Human-readable label shown in log messages.
            base   – Base URL for the listing page.
            suffix – Query-string or path suffix appended to the URL.
            type   – "movie" or "tv" — used to pre-classify items.
            source – Source ID ("hdencode", "ddlbase", "adithd").

        Args:
            scan_type:    "Incremental", "Deep Scan", "Loaded Scan", or "Site Search".
            source_type:  "HDEncode", "DDLBase", or "Adit-HD".
            base_url:     Configured base URL for HDEncode (ignored for other sources).
            flags:        Resolution flags — {"4k", "1080p", "remux", "tv"} mapped to bool.
            search_query: Free-text query used only when scan_type == "Site Search".

        Returns:
            List of source descriptor dicts, or empty list if no flags are set.
        """
        sources = []

        hdencode_requested = scan_type == "Site Search" or source_type == "HDEncode"
        if hdencode_requested and not self.config.get("hdencode_enabled", True):
            return []

        if scan_type == "Site Search":
            if not search_query:
                return []
            import urllib.parse
            safe_query = urllib.parse.quote_plus(search_query)
            sources.append({
                "name": f"Search: {search_query}",
                "base": f"{base_url}/",
                "suffix": f"?s={safe_query}",
                "type": "mixed",
                "source": "hdencode",
                # 'search' is not one of the 4K/Remux/TV categories, so the UI's
                # category toggles never hide explicit search results.
                "category": "search",
            })
        elif source_type == "HDEncode":
            if flags.get("4k"):
                sources.append({"name": "4K Movies", "base": f"{base_url}/quality/2160p/", "suffix": "?tag=movies", "type": "movie", "source": "hdencode", "category": "4k"})
            if flags.get("remux"):
                sources.append({"name": "Remux Movies", "base": f"{base_url}/quality/remux/", "suffix": "?tag=movies", "type": "movie", "source": "hdencode", "category": "remux"})
            if flags.get("tv"):
                sources.append({"name": "TV Packs", "base": f"{base_url}/tag/tv-packs/", "suffix": "", "type": "tv", "source": "hdencode", "category": "tv"})
        elif source_type == "DDLBase":
            if flags.get("4k_webdl"):
                sources.append({"name": "DDLBase WEB-DL 4K", "base": "https://ddlbase.com/cat/movie-webdl-2160p", "suffix": "", "type": "movie", "source": "ddlbase", "category": "4k"})
            if flags.get("4k_remux"):
                sources.append({"name": "DDLBase Remux 4K", "base": "https://ddlbase.com/cat/movie-remux-2160p", "suffix": "", "type": "movie", "source": "ddlbase", "category": "remux"})
            if flags.get("1080p_remux"):
                sources.append({"name": "DDLBase Remux 1080p", "base": "https://ddlbase.com/cat/movie-remux-1080p", "suffix": "", "type": "movie", "source": "ddlbase", "category": "remux"})
        elif source_type == "Adit-HD":
            if flags.get("4k"):
                sources.append({"name": "Adit-HD 4K", "base": "https://adit-hd.com/forums/4k-uhd-movies/", "suffix": "", "type": "movie", "source": "adithd", "category": "4k"})
            if flags.get("remux"):
                sources.append({"name": "Adit-HD Remux", "base": "https://adit-hd.com/forums/remux-movies/", "suffix": "", "type": "movie", "source": "adithd", "category": "remux"})
            if flags.get("tv"):
                sources.append({"name": "Adit-HD TV", "base": "https://adit-hd.com/forums/tv-packs/", "suffix": "", "type": "tv", "source": "adithd", "category": "tv"})

        return sources

    # ── Page crawling ─────────────────────────────────────────────────

    async def _crawl_pages(
        self, sources: List[Dict], pages: int, base_url: str,
        scraper, loop, previously_scanned: Optional[Set[str]] = None,
        early_stop: bool = False,
    ) -> List[Dict]:
        """Crawl listing pages from all sources and collect post URLs.

        Iterates over each source descriptor, fetches each page via cloudscraper
        (run in a thread executor to avoid blocking the event loop), and extracts
        individual post links using ``_select_posts``.

        Duplicate URLs are deduplicated before being added to the result list.
        URLs that appear in ``previously_scanned`` are skipped (incremental mode).
        A small asyncio sleep (0.3 s) is inserted between pages to reduce the
        chance of rate limiting.

        Args:
            sources:  Source descriptors produced by ``_build_sources``.
            pages:    Number of listing pages to fetch per source.
            base_url: HDEncode base URL — used to resolve relative post hrefs.
            scraper:  Pre-created cloudscraper instance shared across the crawl.
            loop:     Running asyncio event loop for ``run_in_executor`` calls.
            previously_scanned: Set of URLs already processed in previous scans.

        Returns:
            List of post dicts: [{"url": str, "type": "movie"|"tv", "source": str}, ...]
        """
        all_posts = []
        skip_urls = previously_scanned or set()
        seen_post_urls: Set[str] = set()  # O(1) dedup instead of O(n) list scan
        skipped_count = 0
        early_stopped = False
        total_pages = len(sources) * pages
        current_page = 0

        for source in sources:
            if self.stop_scan_flag:
                break

            source_name = source["name"]
            source_base = source["base"]
            source_suffix = source["suffix"]
            source_type_hint = source["type"]
            source_id = source.get("source", "hdencode")
            source_category = source.get("category", "")

            self._log(f"Crawling {source_name}...")

            # Cloudflare-blocked sources 403 EVERY page, which used to log one
            # "HTTP 403" warning per page (hundreds per run). Count instead: emit
            # a single aggregated warning per source, back off between blocked
            # pages, and stop the source after a few consecutive blocks rather
            # than firing all N doomed requests with no delay.
            blocked_total = 0
            blocked_streak = 0
            last_block_status = None

            for page_num in range(1, pages + 1):
                if self.stop_scan_flag:
                    break

                current_page += 1
                self._progress(current_page / total_pages, f"Crawling page {page_num}")

                if page_num == 1:
                    url = f"{source_base}{source_suffix}"
                else:
                    if source_id == "ddlbase":
                        url = f"{source_base}/page/{page_num}{source_suffix}"
                    elif source_id == "adithd":
                        url = f"{source_base}page/{page_num}/"
                    else:
                        url = f"{source_base}page/{page_num}/{source_suffix}"

                try:
                    resp = await loop.run_in_executor(None, lambda u=url: scraper.get(u, timeout=15))
                    if resp.status_code != 200:
                        # ONLY a Cloudflare / rate-limit block (403/429/503) is a
                        # "block": it fails every page, so back off and abandon the
                        # source, and (below) mark the crawl incomplete so still-
                        # listed items aren't purged. A 404/other is ordinary
                        # end-of-content — skip that page quietly.
                        if resp.status_code in (403, 429, 503):
                            blocked_total += 1
                            blocked_streak += 1
                            last_block_status = resp.status_code
                            # Back off (grows with the streak) so a blocked source
                            # isn't hammered with N requests in a few seconds.
                            await asyncio.sleep(min(0.5 * blocked_streak, 3.0))
                            if blocked_streak >= 3:
                                # Reuse the ScannerService's existing cancellation
                                # primitive. _process_posts checks this event before
                                # every worker request and cancels queued futures.
                                try:
                                    if source_id == "hdencode" and self.db is not None:
                                        state = "cooldown" if last_block_status == 429 else "blocked"
                                        cooldown = 15 * 60 if last_block_status == 429 else None
                                        self.db.record_source_failure(
                                            "hdencode",
                                            state,
                                            f"http_{last_block_status}",
                                            cooldown_seconds=cooldown,
                                        )
                                except Exception:
                                    # Health persistence must never prevent the
                                    # actual block-detection/stop from taking
                                    # effect (same guarantee as
                                    # backend/source_health.py's own docstring) —
                                    # a DB write failure here must not silently
                                    # swallow the abort via the broader per-page
                                    # except below.
                                    pass
                                self.stop_scan_flag = True
                                self._log(
                                    f"{source_name}: confirmed shared block after "
                                    f"{blocked_streak} consecutive responses; stopping "
                                    "remaining scan work",
                                    "warning",
                                )
                                break  # session can't clear the block this run
                        continue
                    blocked_streak = 0  # a good page resets the streak
                    try:
                        if source_id == "hdencode" and self.db is not None:
                            self.db.record_source_success("hdencode")
                    except Exception:
                        # A health-write failure on a successful page must not
                        # abort parsing that page's posts (they're extracted
                        # further down in this same try block) — see the note
                        # on the block-abort site above.
                        pass

                    soup = BeautifulSoup(resp.content, 'html.parser')
                    posts = self._select_posts(soup, source_id)

                    page_posts = 0   # non-empty post URLs found on this page
                    page_new = 0     # of those, ones not already seen/skipped
                    for post in posts:
                        post_url = post.get('href', '')
                        if post_url.startswith('/'):
                            if source_id == "ddlbase":
                                post_url = f"https://ddlbase.com{post_url}"
                            elif source_id == "adithd":
                                post_url = f"https://adit-hd.com{post_url}"
                            else:
                                post_url = f"{base_url}{post_url}"
                        if not post_url:
                            continue
                        page_posts += 1
                        if post_url in seen_post_urls:
                            continue
                        seen_post_urls.add(post_url)
                        if post_url in skip_urls:
                            skipped_count += 1
                            continue
                        page_new += 1
                        all_posts.append({
                            'url': post_url,
                            'type': source_type_hint,
                            'source': source_id,
                            'category': source_category,
                            'listing_title': post.get_text(' ', strip=True),
                        })

                    # Early-stop: a populated page that yields no new posts means
                    # we've reached content already cached/seen — deeper pages are
                    # older still, so stop crawling this source. Only with a skip
                    # set in play (otherwise every page looks "all new").
                    if early_stop and skip_urls and page_posts > 0 and page_new == 0:
                        self._log(f"{source_name}: reached previously-cached content at page {page_num}, stopping")
                        early_stopped = True
                        break

                    await asyncio.sleep(0.3)
                except Exception as e:
                    self._log(f"Crawl error: {e}", "error")

            if blocked_total:
                # A blocked source's crawl is INCOMPLETE — treat it like an
                # early-stop so the caller does NOT purge still-listed items it
                # simply couldn't reach (they'd reappear as new when the block
                # clears).
                early_stopped = True
                self._log(
                    f"{source_name}: {blocked_total} page(s) blocked "
                    f"(HTTP {last_block_status}) — Cloudflare not cleared; this "
                    f"source's results may be incomplete this run", "warning")

        if skipped_count:
            self._log(f"Skipped {skipped_count} previously scanned URLs")

        # Expose every listing URL seen this crawl (new + skipped) so callers can
        # refresh "last seen" on still-listed items without re-scraping them.
        self._last_crawl_seen_urls = set(seen_post_urls)
        # A crawl that stopped early never visited deeper pages, so its seen-set
        # is partial — the caller must not age out items it simply didn't revisit.
        self._last_crawl_early_stopped = early_stopped

        return all_posts

    @staticmethod
    def _select_posts(soup, source_id: str):
        """Select post link elements from a listing page based on source.

        Each source uses different HTML structures; this method provides
        multiple CSS selector fallbacks per source for resilience.
        """
        if source_id == "ddlbase":
            return (soup.select('div.movie_title_list > a[href*="/post/"]') or
                    soup.select('a[href*="/post/"]'))
        elif source_id == "adithd":
            return (soup.select('.structItem-title a[href*="threads/"]') or
                    soup.select('.contentRow-title a') or
                    soup.select('a[href*="threads/"]'))
        else:
            return (soup.select('div.data h5 a') or
                    soup.select('div.data a') or
                    soup.select('h2.entry-title a') or
                    soup.select('.post-title a') or
                    soup.select('article a[rel="bookmark"]'))

    # ── Post processing ───────────────────────────────────────────────

    def _parse_hdencode_listing_candidate(self, post_info: Dict):
        """Return a lightweight ParsedRelease from listing text, or None."""
        title = (post_info.get("listing_title") or "").strip()
        if post_info.get("source") != "hdencode" or not title:
            return None
        try:
            parser = getattr(self, "_hdencode_listing_parser", None)
            if parser is None:
                from backend.sources.hdencode import HDEncodeSource
                parser = HDEncodeSource()
                self._hdencode_listing_parser = parser
            mode = "tv" if post_info.get("type") == "tv" else "movies"
            return parser.parse_release({
                "title": title,
                "url": post_info.get("url", ""),
                "mode": mode,
                "article_html": "",
            })
        except Exception:
            logger.debug("Could not parse HDEncode listing candidate %r", title, exc_info=True)
            return None

    def _should_hydrate_listing_candidate(self, post_info: Dict) -> bool:
        """Whether a detail request is still needed for this listing candidate.

        Fail-open by design. Listing text does not always expose Dolby Vision,
        file size, episode count, or codec/preference metadata. A candidate is
        skipped only when available listing evidence is sufficient under the
        user's active upgrade rules.
        """
        if post_info.get("source") != "hdencode":
            return True

        url = post_info.get("url", "")
        if url and url in self.download_history:
            return False

        release = self._parse_hdencode_listing_candidate(post_info)
        if release is None or not getattr(release, "display_title", None):
            return True

        try:
            title = release.display_title
            season = release.season
            resolution = release.resolution or ""
            dovi = bool(release.is_dovi)
            title_key = normalize_title(title)
            raw_size = str(getattr(release, "size", "") or "").strip()
            size_known = raw_size.lower() not in {
                "", "?", "-", "unknown", "n/a", "none"
            }

            if (not title_key or not resolution
                    or (not release.is_tv and not release.year)
                    or (release.is_tv and season is None)):
                return True

            history_key = (
                f"{title_key}|S{season}"
                if season is not None
                else title_key
            )
            prior_entries = self._downloaded_titles_lookup.get(history_key) or []
            if prior_entries:
                # Compare against the best owned quality, not merely the latest row.
                best = max(
                    prior_entries,
                    key=lambda entry: (
                        _res_rank(entry.get("resolution")),
                        bool(entry.get("dovi")),
                        entry.get("downloaded_at", ""),
                    ),
                )
                incoming_rank = _res_rank(resolution)
                prior_rank = _res_rank(best.get("resolution"))
                prior_dovi = bool(best.get("dovi"))

                if incoming_rank > prior_rank:
                    return True
                if incoming_rank < prior_rank:
                    return False
                if dovi and not prior_dovi:
                    return True
                if (
                    self.config.get("pref_hevc", False)
                    or self.config.get("pref_hdr10plus", False)
                ):
                    # Download history records resolution/DV but not enough
                    # listing-stage codec metadata to prove this is not the
                    # user's configured same-resolution preference upgrade.
                    return True
                if (self.config.get("rule_dv", True)
                        and not prior_dovi and not dovi):
                    # Absence of a DV token in listing text is not proof that
                    # the detail page lacks DV.
                    return True
                # Download-history sibling semantics intentionally ignore size.
                return False

            plex_index = getattr(self.plex, "plex_index", None) or {}
            if not plex_index.get("all_items"):
                return True

            web_item = {
                "display_title": title,
                "year": release.year or 0,
                "res": resolution or "?",
                "size": raw_size or "?",
                "dovi": dovi,
                "hdr": (
                    release.hdr_format
                    or ("HDR" if release.is_hdr else "SDR")
                ),
                "url": url,
                "imdb_id": release.imdb_id,
                "is_tv": bool(release.is_tv),
                "season": season,
                "episodes": None,
                "search_key": title_key,
                "episode_number": release.episode,
            }

            if web_item["is_tv"]:
                matches, is_uncertain = self.matching.find_tv_season_matches(
                    web_item,
                    plex_index,
                )
            else:
                matches, is_uncertain = self.matching.find_movie_matches(
                    web_item,
                    plex_index,
                )
            if is_uncertain or not matches:
                return True

            same_res = [
                match for match in matches
                if match.get("res") == resolution
            ]
            if same_res:
                if (
                    self.config.get("pref_hevc", False)
                    or self.config.get("pref_hdr10plus", False)
                ):
                    return True

                if (
                    self.config.get("rule_dv", True)
                    and not dovi
                    and any(not bool(match.get("dovi")) for match in same_res)
                ):
                    return True

                if not size_known:
                    if web_item["is_tv"]:
                        return True
                    if (
                        resolution == "1080p"
                        and self.config.get("rule_1080_1080", True)
                    ):
                        return True
                    if (
                        resolution in ("4K", "2160p")
                        and self.config.get("rule_4k_4k", True)
                    ):
                        return True

            if web_item["is_tv"]:
                status_str, _color, _info, is_upgrade = (
                    self.matching.calculate_tv_upgrade_status(
                        web_item,
                        matches[0],
                    )
                )
                return bool(is_upgrade) or status_str == STATUS_MISSING

            status_str, _color, _info, _plex_id = (
                self.matching.calculate_movie_upgrade_status(
                    web_item,
                    matches,
                )
            )
            return (
                status_str == STATUS_MISSING
                or "UPGRADE" in status_str.upper()
            )
        except Exception:
            logger.debug(
                "Listing-stage relevance check failed open for %s",
                post_info.get("listing_title", "?"),
                exc_info=True,
            )
            return True

    async def _process_posts(self, all_posts: List[Dict], scraper, num_threads: int):
        discovered_posts = len(all_posts)
        eligible_posts = [
            post for post in all_posts
            if self._should_hydrate_listing_candidate(post)
        ]
        skipped = discovered_posts - len(eligible_posts)
        if skipped:
            self._log(
                f"Lazy hydration skipped {skipped}/{discovered_posts} conclusive "
                "already-owned candidate(s) before detail fetch"
            )
        processed = 0
        total_posts = len(eligible_posts)
        if total_posts == 0:
            self._log(
                f"Processing complete: 0 items created from {discovered_posts} posts "
                f"({skipped} detail request(s) avoided)"
            )
            return

        def process_post(post_info):
            if self.stop_scan_flag:
                return None
            url = post_info['url']
            post_source = post_info.get('source', 'hdencode')
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                details = self.scrapers.scrape_details(url, headers, scraper)
                if not details:
                    return None
                is_tv = details.get('is_tv', False) or post_info['type'] == 'tv'
                details['source'] = post_source
                details['category'] = post_info.get('category', '')
                return {'details': details, 'is_tv': is_tv, 'url': url}
            except Exception as e:
                logger.debug("Error processing post %s: %s", url, e)
                return None

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(process_post, post) for post in eligible_posts]
            for future in as_completed(futures):
                if self.stop_scan_flag:
                    # Cancel queued (not-yet-started) futures so the executor
                    # exits quickly once the currently running workers finish.
                    for f in futures:
                        f.cancel()
                    break
                try:
                    result = future.result()  # already completed — no timeout needed
                except Exception as e:
                    logger.debug("Post processing worker failed: %s", e)
                    processed += 1
                    continue
                processed += 1
                self._progress(processed / total_posts, f"Processing {processed}/{total_posts}")
                if result:
                    item = self._create_media_item(result)
                    if item:
                        with self._items_lock:
                            item.id = f"item_{self._item_counter}"
                            self._item_counter += 1
                            self.items.append(item)

        self._log(
            f"Processing complete: {len(self.items)} items created from "
            f"{discovered_posts} posts ({skipped} detail request(s) avoided)"
        )

    # ── Item creation ─────────────────────────────────────────────────

    @staticmethod
    def _parse_rating(value) -> float:
        """Safely parse a rating value to float, returning 0.0 on failure."""
        if not value or value == '-':
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    def _download_status_for(self, url: str, title: str, season: Optional[int],
                             resolution: str, *, hdr: str = "", dovi: bool = False):
        """Resolve download-history status + prior_grab for a candidate item.

        Shared by the live scan (``_create_media_item``) and the cache re-match
        (``rematch_cache``) so both treat 'already grabbed' identically. Returns
        ``(ScanStatus, prior_grab dict | None)``.

        - The EXACT release you grabbed (matched by URL) → DOWNLOADED.
        - Another release of the same title that is NOT a quality upgrade over
          the grab (same-or-worse resolution, no new Dolby Vision) →
          DOWNLOADED_SIMILAR (you effectively have it), with the grab note.
        - A genuine upgrade sibling (higher resolution, or gains DV the grab
          lacked) stays MISSING + note, so it's still surfaced as worth grabbing.
        """
        status = ScanStatus.DOWNLOADED if url in self.download_history else ScanStatus.MISSING
        prior_grab = None
        normalized = normalize_title(title)
        if normalized and status == ScanStatus.MISSING:
            key = f"{normalized}|S{season}" if season is not None else normalized
            entries = self._downloaded_titles_lookup.get(key)
            if entries:
                best = max(entries, key=lambda e: e.get('downloaded_at', ''))
                prior_grab = {
                    'resolution': best.get('resolution') or '',
                    'size': best.get('size') or '',
                    'downloaded_at': best.get('downloaded_at', ''),
                    'hdr': best.get('hdr') or '',
                    'dovi': bool(best.get('dovi')),
                }
                ir, gr = _res_rank(resolution), _res_rank(best.get('resolution'))
                is_upgrade = ir > gr or (ir == gr and bool(dovi) and not bool(best.get('dovi')))
                if not is_upgrade:
                    status = ScanStatus.DOWNLOADED_SIMILAR
        return status, prior_grab

    def _create_media_item(self, result: Dict) -> Optional[MediaItem]:
        """Convert a scraped post result into a MediaItem.

        Handles:
        - Download-history status (URL match and title+season lookup for TV).
        - Per-episode size calculation for multi-episode TV packs.
        - Safe rating parsing via ``_parse_rating``.
        - Group key generation used later by ``detect_duplicate_groups``.

        The returned item's ``id`` field is left blank ("") and assigned
        under ``_items_lock`` by the caller to avoid race conditions.

        Args:
            result: Dict with keys "details" (scraped metadata dict) and "url".

        Returns:
            A populated MediaItem, or None if a construction error occurs.
        """
        try:
            details = result['details']
            url = result['url']

            normalized = normalize_title(details.get('display_title', ''))
            season = details.get('season')
            status, prior_grab = self._download_status_for(
                url, details.get('display_title', ''), season, details.get('res', ''),
                hdr=details.get('hdr', ''), dovi=details.get('dovi', False))

            # Compute per-episode size for TV packs
            raw_size = details.get('size', '?')
            episodes = details.get('episodes')
            size_display = raw_size
            if episodes and episodes > 1 and raw_size and raw_size not in ('?', '-', 'Unknown'):
                try:
                    s = str(raw_size).upper().replace(' ', '')
                    if 'TB' in s:
                        total_gb = float(re.sub(r'[A-Z]+', '', s)) * 1024
                    elif 'GB' in s:
                        total_gb = float(re.sub(r'[A-Z]+', '', s))
                    elif 'MB' in s:
                        total_gb = float(re.sub(r'[A-Z]+', '', s)) / 1024
                    else:
                        total_gb = 0
                    if total_gb > 0:
                        per_ep = total_gb / episodes
                        size_display = f"{raw_size} (~{per_ep:.1f} GB/ep)"
                except (ValueError, TypeError):
                    pass

            return MediaItem(
                id="",  # Assigned under lock at append time
                title=details.get('display_title', 'Unknown'),
                year=details.get('year', 0),
                season=season,
                episodes=episodes,
                rating=self._parse_rating(details.get('rating')),
                status=status,
                status_text=STATUS_TEXTS[status],
                color=STATUS_COLORS[status],
                url=url,
                resolution=details.get('res', '?'),
                size=size_display,
                hdr=details.get('hdr', 'SDR'),
                dovi=details.get('dovi', False),
                genres=details.get('genres', []),
                language=details.get('language', ''),
                web_data=details,
                group_key=f"{normalized}|{details.get('year', 0) or 0}|S{season or 0}",
                prior_grab=prior_grab,
                poster_path=details.get('poster_path'),
                imdb_id=details.get('imdb_id'),
                description=details.get('description', ''),
                posted_date=details.get('posted_date'),
                category=details.get('category', ''),
            )
        except Exception as e:
            self._log(f"Error creating media item: {e}", "warning")
            return None

    # ── Cache re-match (no re-scrape) ─────────────────────────────────

    def _media_item_from_dict(self, d: Dict[str, Any]) -> Optional[MediaItem]:
        """Reconstruct a MediaItem from a cached result dict (the JSON stored in
        ``background_scan_cache``) so it can be re-matched without re-scraping."""
        try:
            try:
                status = ScanStatus(d.get('status', 'missing'))
            except ValueError:
                status = ScanStatus.MISSING
            return MediaItem(
                id=str(d.get('id', '') or ''),
                title=d.get('title', '') or '',
                year=d.get('year', 0) or 0,
                season=d.get('season'),
                episodes=d.get('episodes'),
                rating=d.get('rating', 0.0) or 0.0,
                rt_score=d.get('rt_score'),
                status=status,
                status_text=d.get('status_text', '') or '',
                color=d.get('color', '') or '',
                resolution=d.get('resolution', '') or '',
                size=d.get('size', '') or '',
                hdr=d.get('hdr', '') or '',
                dovi=bool(d.get('dovi', False)),
                genres=d.get('genres', []) or [],
                language=d.get('language', '') or '',
                url=d.get('url', '') or '',
                plex_info=d.get('plex_info', '-') or '-',
                plex_versions=d.get('plex_versions', '[]') or '[]',
                plex_rating_key=d.get('plex_rating_key'),
                poster_path=d.get('poster_path'),
                imdb_id=d.get('imdb_id'),
                description=d.get('description', '') or '',
                posted_date=d.get('posted_date'),
                web_data=d.get('web_data', {}) or {},
                group_key=d.get('group_key', '') or '',
                prior_grab=d.get('prior_grab'),
                category=d.get('category', '') or '',
            )
        except Exception:
            return None

    def rematch_cache(self) -> int:
        """Re-evaluate every cached item's library/download status against the
        CURRENT Plex index and download history, WITHOUT re-scraping.

        Cheap and in-memory — the expensive part (scraping detail pages) is
        skipped; this only re-runs matching. Only rows whose status/info actually
        changed are written, and ``last_seen`` is left untouched so retention is
        unaffected. Call while the Plex index is loaded (e.g. right after a
        background scan). Returns the number of rows updated."""
        if self.db is None:
            return 0
        rows = self.db.get_background_cache(limit=1_000_000)
        if not rows:
            return 0

        # Fresh download history so Downloaded/Grabbed reflect recent grabs.
        self.download_history = self._load_download_history()
        self.matching.app.download_history = self.download_history

        # When no Plex index is loaded this run (transient outage / empty load),
        # the matcher can't restore IN_LIBRARY, so we must NOT clear each item's
        # cached Plex match or downgrade owned titles to Missing. Download history
        # may still upgrade a row (Missing -> Downloaded); that path is preserved.
        have_plex = bool(self.plex and self.plex.plex_index.get("all_items"))
        library_states = (ScanStatus.IN_LIBRARY, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE)

        items: List[MediaItem] = []
        data_by_url: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            try:
                d = json.loads(row.get('data') or '{}')
            except Exception:
                continue
            item = self._media_item_from_dict(d)
            if item is None or not item.url:
                continue
            # Re-apply download-history status + prior_grab.
            dl_status, item.prior_grab = self._download_status_for(
                item.url, item.title, item.season, item.resolution,
                hdr=item.hdr, dovi=item.dovi)
            if have_plex:
                # Reset to download status + clear Plex info; the match below
                # re-applies IN_LIBRARY/UPGRADE for still-owned items.
                item.status = dl_status
                item.status_text = STATUS_TEXTS.get(item.status, item.status_text)
                item.color = STATUS_COLORS.get(item.status, item.color)
                item.plex_info = "-"
                item.plex_versions = "[]"
            elif (dl_status in (ScanStatus.DOWNLOADED, ScanStatus.DOWNLOADED_SIMILAR)
                  and item.status not in library_states):
                # No Plex this run: let a real download UPGRADE the row, but never
                # downgrade a cached IN_LIBRARY/UPGRADE to Missing.
                item.status = dl_status
                item.status_text = STATUS_TEXTS.get(item.status, item.status_text)
                item.color = STATUS_COLORS.get(item.status, item.color)
            # else (no Plex, no fresh download): keep cached status + Plex info.
            items.append(item)
            data_by_url[item.url] = d

        if not items:
            return 0

        # Run the same Plex matcher over the reconstructed items.
        with self._items_lock:
            saved_items = self.items
            self.items = items
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._match_against_plex("Deep Scan"))
            finally:
                loop.close()
        except Exception:
            logger.exception("Cache re-match: Plex matching failed")
        finally:
            with self._items_lock:
                self.items = saved_items

        # Persist only rows whose status/info changed (preserve last_seen).
        updates = []
        for item in items:
            d = data_by_url.get(item.url)
            if d is None:
                continue
            new_status = item.status.value if isinstance(item.status, ScanStatus) else str(item.status)
            if (d.get('status') != new_status
                    or d.get('plex_info', '-') != item.plex_info
                    or d.get('plex_versions', '[]') != item.plex_versions
                    or d.get('prior_grab') != item.prior_grab):
                d['status'] = new_status
                d['status_text'] = item.status_text
                d['color'] = item.color
                d['plex_info'] = item.plex_info
                d['plex_versions'] = item.plex_versions
                d['prior_grab'] = item.prior_grab
                updates.append({'url': item.url, 'status': new_status,
                                'data': json.dumps(d, default=str)})

        if updates:
            self.db.update_background_status(updates)
        logger.info("Cache re-match: %d of %d cached item(s) updated", len(updates), len(items))
        return len(updates)

    # ── Plex matching ─────────────────────────────────────────────────

    async def _match_against_plex(self, scan_type: str = "Deep Scan"):
        """Compare all scan results against the Plex library index.

        Updates each MediaItem's status (IN_LIBRARY, UPGRADE, DV_UPGRADE)
        and plex_info field based on matching results.
        """
        plex_index = self.plex.plex_index
        if not plex_index["all_items"]:
            self._log("No Plex data available, skipping matching", "warning")
            return

        with self._items_lock:
            items_snapshot = list(self.items)
        total = len(items_snapshot)
        for idx, item in enumerate(items_snapshot):
            if self.stop_scan_flag:
                break
            self._progress(idx / total, f"Matching {idx}/{total}")

            if item.status in (ScanStatus.DOWNLOADED, ScanStatus.DOWNLOADED_SIMILAR):
                continue

            web_item = {
                'display_title': item.title,
                'year': item.year,
                'res': item.resolution,
                'size': item.web_data.get('size', item.size),
                'dovi': item.dovi,
                'hdr': item.hdr,
                'url': item.url,
                'imdb_id': item.web_data.get('imdb_id'),
                'is_tv': item.season is not None,
                'season': item.season,
                'episodes': item.episodes,
                'search_key': normalize_title(item.title),
                'episode_number': item.web_data.get('episode_number'),
            }

            try:
                if web_item['is_tv']:
                    matches, is_uncertain = self.matching.find_tv_season_matches(web_item, plex_index)
                else:
                    matches, is_uncertain = self.matching.find_movie_matches(web_item, plex_index)

                if not matches and web_item['is_tv'] and item.season:
                    # Season not in Plex — check if the show exists with other seasons
                    show_in_plex = []
                    imdb_id = web_item.get('imdb_id')
                    if imdb_id and imdb_id in plex_index["by_imdb"]:
                        show_in_plex = plex_index["by_imdb"][imdb_id]
                    elif web_item.get('search_key') and web_item['search_key'] in plex_index["by_title"]:
                        # Filter by year tolerance and TV-only (must have seasons)
                        web_year = web_item.get('year', 0)
                        year_tol = self.config.get('year_tolerance', 1)
                        show_in_plex = [
                            p for p in plex_index["by_title"][web_item['search_key']]
                            if p.get('season') is not None  # must be a TV season, not a movie
                            and (not web_year or not p.get('year')
                                 or abs(web_year - p.get('year', 0)) <= year_tol)
                        ]

                    if show_in_plex:
                        # Only count real seasons (≥1) — S0 (Specials/movies) alone is
                        # not meaningful evidence that a regular season is missing.
                        real_seasons = [
                            p for p in show_in_plex if (p.get('season') or 0) >= 1
                        ]
                        if real_seasons:
                            item.status = ScanStatus.MISSING_SEASON
                            item.status_text = STATUS_TEXTS[ScanStatus.MISSING_SEASON]
                            item.color = STATUS_COLORS[ScanStatus.MISSING_SEASON]
                            have_seasons = sorted(set(
                                s.get('season') for s in real_seasons
                                if s.get('season') is not None
                            ))
                            item.plex_info = "Have: " + ", ".join(f"S{s}" for s in have_seasons)
                        if not item.imdb_id:
                            item.imdb_id = show_in_plex[0].get('imdb_id')
                    else:
                        # Plex doesn't know about the show, but check if another
                        # season was previously downloaded via ScanHound.
                        title_key = web_item.get('search_key', '')
                        if title_key:
                            dl_seasons = []
                            for k in self._downloaded_titles_lookup:
                                if not k.startswith(f"{title_key}|S"):
                                    continue
                                if k == f"{title_key}|S{item.season}":
                                    continue
                                try:
                                    sn = int(k.split("|S", 1)[1])
                                except (ValueError, IndexError):
                                    continue
                                if sn >= 1:
                                    dl_seasons.append(sn)
                            dl_seasons.sort()
                            if dl_seasons:
                                item.status = ScanStatus.MISSING_SEASON
                                item.status_text = STATUS_TEXTS[ScanStatus.MISSING_SEASON]
                                item.color = STATUS_COLORS[ScanStatus.MISSING_SEASON]
                                item.plex_info = "Downloaded: " + ", ".join(f"S{s}" for s in dl_seasons)

                if matches:
                    # Calculate detailed upgrade status via matching engine
                    if web_item['is_tv']:
                        status_str, color, info, is_upgrade = self.matching.calculate_tv_upgrade_status(
                            web_item, matches[0]
                        )
                        item.plex_rating_key = str(matches[0].get("rating_key")) if matches[0].get("rating_key") is not None else None
                    else:
                        status_str, color, info, plex_id = self.matching.calculate_movie_upgrade_status(
                            web_item, matches
                        )
                        is_upgrade = "UPGRADE" in status_str
                        item.plex_rating_key = str(plex_id) if plex_id is not None else None

                    item.plex_info = info

                    # Build structured per-version data for QML
                    def _version_dict(m):
                        return {
                            "res": m.get("res", "?"),
                            "size": m.get("size", 0),
                            "dovi": bool(m.get("dovi", False)),
                            "hdr": bool(m.get("hdr") and m.get("hdr") not in ("SDR", "")),
                        }
                    if web_item['is_tv']:
                        tv_dict = _version_dict(matches[0])
                        tv_dict["episode_count"] = matches[0].get("episode_count", 0)
                        item.plex_versions = json.dumps([tv_dict])
                    else:
                        sorted_m = sorted(
                            matches,
                            key=lambda x: (x.get("dovi", False), x.get("size", 0)),
                            reverse=True,
                        )
                        # Deduplicate versions from multiple libraries
                        # (same file in "Movies 4K" + "Movies 1080p" libraries)
                        seen_versions = set()
                        unique_versions = []
                        for m in sorted_m:
                            vd = _version_dict(m)
                            key = (vd["res"], vd["size"], vd["dovi"], vd["hdr"])
                            if key not in seen_versions:
                                seen_versions.add(key)
                                unique_versions.append(vd)
                        item.plex_versions = json.dumps(unique_versions)

                    if is_upgrade:
                        if "DV" in status_str:
                            item.status = ScanStatus.DV_UPGRADE
                            item.status_text = STATUS_TEXTS[ScanStatus.DV_UPGRADE]
                            item.color = STATUS_COLORS[ScanStatus.DV_UPGRADE]
                        else:
                            item.status = ScanStatus.UPGRADE
                            item.status_text = STATUS_TEXTS[ScanStatus.UPGRADE]
                            item.color = STATUS_COLORS[ScanStatus.UPGRADE]
                    elif status_str == STATUS_MISSING:
                        # Matching engine returned Missing (e.g. strict resolution mismatch)
                        # even though a Plex entry was found — keep item as Missing
                        pass
                    else:
                        item.status = ScanStatus.IN_LIBRARY
                        item.status_text = STATUS_TEXTS[ScanStatus.IN_LIBRARY]
                        item.color = STATUS_COLORS[ScanStatus.IN_LIBRARY]

                    if not item.imdb_id and matches[0].get('imdb_id'):
                        item.imdb_id = matches[0]['imdb_id']

            except Exception as e:
                logger.debug(f"Match error for '{item.title}': {e}")

    # ── Missing-season detection (post-Plex) ────────────────────────

    def _mark_missing_seasons_from_scan(self):
        """Mark MISSING TV items as MISSING_SEASON when other seasons are downloaded.

        Runs after _match_against_plex as a safety net — catches cases where:
        - Plex data was unavailable (early return in _match_against_plex)
        - Legacy download entries lack normalized_title in the DB
        - A sibling season was downloaded in the current scan session

        Only touches items still at MISSING status (won't override Plex-based
        MISSING_SEASON detection).
        """
        # Snapshot items under lock to avoid data race
        with self._items_lock:
            items_snapshot = list(self.items)

        # Collect downloaded seasons from current scan items
        scan_downloaded: Dict[str, Set[int]] = defaultdict(set)
        for item in items_snapshot:
            if item.status == ScanStatus.DOWNLOADED and item.season is not None:
                title_key = normalize_title(item.title)
                if title_key:
                    scan_downloaded[title_key].add(item.season)

        # Also include _downloaded_titles_lookup (from DB)
        for k in self._downloaded_titles_lookup:
            if "|S" in k:
                try:
                    title_part, season_str = k.split("|S", 1)
                    scan_downloaded[title_part].add(int(season_str))
                except (ValueError, IndexError):
                    pass

        # Mark MISSING TV items that have downloaded siblings
        for item in items_snapshot:
            if item.status != ScanStatus.MISSING or not item.season:
                continue

            title_key = normalize_title(item.title)
            if not title_key:
                continue

            # Exclude S0 — movie downloads shouldn't trigger "Missing Season!" for TV
            sibling_seasons = {s for s in scan_downloaded.get(title_key, set()) if s >= 1} - {item.season}
            if sibling_seasons:
                sorted_seasons = sorted(sibling_seasons)
                item.status = ScanStatus.MISSING_SEASON
                item.status_text = STATUS_TEXTS[ScanStatus.MISSING_SEASON]
                item.color = STATUS_COLORS[ScanStatus.MISSING_SEASON]
                item.plex_info = "Downloaded: " + ", ".join(
                    f"S{s}" for s in sorted_seasons
                )

    # ── Metadata enrichment ───────────────────────────────────────────

    async def _enrich_metadata_async(self):
        """Enrich scan results with TMDB metadata, OMDb ratings, and RT scores.

        Delegates to MetadataEnricher (backend/metadata_enricher.py), which
        runs 4 parallel worker threads to fetch descriptions, posters,
        IMDb ratings, vote counts, and Rotten Tomatoes scores for items
        that are missing metadata.
        """
        await self._enricher.enrich(
            self.items,
            stop_flag_fn=lambda: self.stop_scan_flag,
            progress_fn=self._progress,
            log_fn=self._log,
        )

    # ── Grouping ──────────────────────────────────────────────────────

    def _assign_group_keys(self):
        """(Re)derive each item's group_key from its CURRENT (post-enrichment)
        title/year/season, using the canonical uniform recipe:

            ``{normalized_title}|{year or 0}|S{season or 0}``

        Movies key on title+year (season 0); TV keys on title+year+season. This
        runs after enrichment so a corrected title (e.g. a release-group leak
        fixed to the real name) regroups under the right key — the read-time
        overlays in api/routes/results.py reconstruct this exact format.
        """
        for item in self.items:
            norm = normalize_title(item.title)
            item.group_key = f"{norm}|{item.year or 0}|S{item.season or 0}"

    def detect_duplicate_groups(self, filtered_items: Optional[List[MediaItem]] = None):
        """Group items by normalized title to detect duplicates and multi-season entries.

        Movies are grouped by title+year. TV shows with the same title but
        different seasons are grouped together. Sets is_duplicate_group and
        group_key on each MediaItem.
        """
        items = filtered_items or self.filtered_items

        tv_by_title: Dict[str, List[MediaItem]] = defaultdict(list)
        movie_groups: Dict[str, List[MediaItem]] = defaultdict(list)

        for item in items:
            title_norm = normalize_title(item.title)
            if item.season is not None:
                tv_by_title[title_norm].append(item)
            else:
                key = f"{title_norm}|{item.year or 0}|S0"
                item.group_key = key
                movie_groups[key].append(item)

        groups = dict(movie_groups)

        for title_norm, group_items in tv_by_title.items():
            distinct_seasons = set(i.season for i in group_items)
            if len(distinct_seasons) > 1:
                key = f"{title_norm}|TV"
                group_items.sort(key=lambda i: (i.season or 0, i.resolution))
                for item in group_items:
                    item.group_key = key
                groups[key] = group_items
            else:
                season = group_items[0].season or 0
                key = f"{title_norm}|S{season}"
                for item in group_items:
                    item.group_key = key
                groups[key] = group_items

        for key, group_items in groups.items():
            is_group = len(group_items) > 1
            for item in group_items:
                item.is_duplicate_group = is_group
            if is_group and key not in self.grouped_items:
                self.expanded_groups.add(key)

        self.grouped_items = dict(groups)

    # ── Helpers ───────────────────────────────────────────────────────

    _EPOCH = datetime(2000, 1, 1)
    _DATE_FORMATS = [
        "%B %d, %Y at %I:%M %p",
        "%B %d %Y at %I:%M %p",
        "%b %d, %Y at %I:%M %p",
    ]

    @classmethod
    def _posted_date_sort_key(cls, item: 'MediaItem') -> datetime:
        """Parse posted_date string into a datetime for sorting."""
        raw = item.posted_date
        if not raw:
            return cls._EPOCH
        for fmt in cls._DATE_FORMATS:
            try:
                return datetime.strptime(raw.strip(), fmt)
            except ValueError:
                continue
        return cls._EPOCH

    def _load_download_history(self) -> Set[str]:
        """Load download history URLs and build a title-based lookup table.

        Returns a set of previously downloaded URLs. Also populates
        _downloaded_titles_lookup for title+season matching (handles
        cases where URLs change between scans).
        """
        try:
            with self.db.transaction() as conn:
                if not conn:
                    return set()
                rows = conn.execute(
                    "SELECT url FROM downloads WHERE COALESCE(status, 'completed') != 'failed'"
                ).fetchall()
                urls = {row[0] for row in rows}

                # Also build title-based lookup for TV show matching
                # (URLs change between scans, but titles + seasons don't)
                # Done in the same transaction to get a consistent snapshot.
                self._downloaded_titles_lookup.clear()
                title_rows = conn.execute(
                    "SELECT normalized_title, season, resolution, size, url, date_added, hdr, dovi "
                    "FROM downloads WHERE normalized_title IS NOT NULL "
                    "AND COALESCE(status, 'completed') != 'failed'"
                ).fetchall()

            for row in title_rows:
                norm_title = row[0]
                season = row[1]
                resolution = row[2] or ''
                size = row[3] or ''
                key = f"{norm_title}|S{season}" if season is not None else norm_title
                self._downloaded_titles_lookup.setdefault(key, []).append({
                    'resolution': resolution,
                    'size': size,
                    'url': row[4] if len(row) > 4 else '',
                    'downloaded_at': row[5] if len(row) > 5 else '',
                    'hdr': row[6] if len(row) > 6 else '',
                    'dovi': bool(row[7]) if len(row) > 7 else False,
                })

            return urls
        except Exception as e:
            logger.error("Failed to load download history: %s", e)
            return set()
