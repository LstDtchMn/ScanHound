"""DetailScraper — HDEncode post detail scraping.

Provides the DetailScraper class used by WebScrapers to extract structured
media metadata from HDEncode (and compatible) post pages.  All methods are
synchronous (blocking) and designed to run in thread-pool executors.
"""

import logging
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from typing import Callable, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from backend import release_grammar
from backend.models import ScrapeResult
from backend.hdencode_coordinator import (
    HDEncodeRequestCancelled,
    HDEncodeTrafficDenied,
    configure_hdencode_coordinator,
    get_hdencode_coordinator,
)
from backend.hdencode_transport import create_source_http_client
from backend.rename import llm_identify as _llm
from backend.sources.hdencode_feed_parser import HEVC_TOKEN_RE

logger = logging.getLogger(__name__)

#: Version stamp for what DETAIL hydration extracts — persisted to
#: hdencode_candidates.detail_parse_version at completion, compared by
#: reconcile_derived_versions: completed rows stamped with anything else are
#: marked refetch_required and requeued, so a capability change here heals
#: gradually (hydration-limit per cycle) instead of instantly.
#: Deliberately DECOUPLED from release_grammar.GRAMMAR_VERSION (round-13): a
#: detail-only extraction change must not force an offline reparse of every
#: feed fact, and a grammar change already reaches detail rows through the
#: refetch leg because the stamps then differ too.
#: v2: episode_end (glued-range) and hevc codec evidence join the payload.
DETAIL_PARSE_VERSION = "hdencode-detail-v2"

# HDEncode pacing and authorization now live in HDEncodeTrafficCoordinator.


def _detail_source_kind(url: str) -> str:
    """Classify detail traffic by parsed hostname.

    DDLBase and Adit-HD share the parsing facade but remain outside the
    HDEncode traffic coordinator. Unknown and malformed URLs fail closed to
    the HDEncode policy.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""

    if host == "ddlbase.com" or host.endswith(".ddlbase.com"):
        return "ddlbase"
    if host == "adit-hd.com" or host.endswith(".adit-hd.com"):
        return "adithd"
    return "hdencode"


class _DetailRequestCancelled(Exception):
    """Internal control-flow signal; never exposed as a scrape failure."""


def _is_cancelled(stop_requested: Optional[Callable[[], bool]]) -> bool:
    if stop_requested is None:
        return False
    try:
        return bool(stop_requested())
    except Exception:
        return True


def _interruptible_sleep(
    seconds: float,
    stop_requested: Optional[Callable[[], bool]],
) -> None:
    if seconds <= 0:
        return
    if stop_requested is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        if _is_cancelled(stop_requested):
            raise _DetailRequestCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


class DetailScraper:
    """Scrapes media metadata from HDEncode post pages."""

    def __init__(self, parent_app):
        """Initialize with reference to the parent app (config, helpers, logging).

        Args:
            parent_app: AppService instance (provides config, parse_size,
                        clean_string, safe_log).
        """
        self.app = parent_app
        configure_hdencode_coordinator(
            getattr(parent_app, "config", {}),
            getattr(parent_app, "db", None),
        )

    def scrape_details(
        self,
        url,
        headers,
        scraper=None,
        *,
        stop_requested: Optional[Callable[[], bool]] = None,
    ) -> Optional[ScrapeResult]:
        """Scrape movie/TV show details from an HDEncode post page.

        Extracts filename, title, year, resolution, file size, HDR/DV flags,
        IMDb ID, and TV season/episode info from the page content.

        Handles:
            - Movies (Title.Year format)
            - TV season packs (Show.S01 format)
            - Single TV episodes (Show.S01E01 format)
            - Multi-episode packs with mirrors (deduplicates via unique eps)
            - cp437 mojibake repair on Windows-sourced filenames

        Args:
            url: HDEncode detail page URL.
            headers: HTTP headers for the request.
            scraper: Optional pre-created cloudscraper instance (avoids
                     creating a new one per call for batch processing).

        Returns:
            dict with parsed media metadata, or None on failure.
        """
        try:
            source_kind = _detail_source_kind(url)
            hdencode_request = source_kind == "hdencode"

            # Retry logic for robust connection
            max_retries = 3
            resp = None
            last_error = None

            for attempt in range(max_retries):
                if _is_cancelled(stop_requested):
                    return None
                try:
                    if hdencode_request:
                        with get_hdencode_coordinator().request(
                            "detail",
                            stop_requested=stop_requested,
                        ):
                            if _is_cancelled(stop_requested):
                                return None
                            active_scraper = scraper or create_source_http_client(
                                hdencode=True
                            )
                            resp = active_scraper.get(
                                url, headers=headers, timeout=20
                            )
                        get_hdencode_coordinator().observe_http_status(
                            resp.status_code
                        )
                    else:
                        active_scraper = scraper or create_source_http_client(
                            hdencode=False
                        )
                        resp = active_scraper.get(
                            url, headers=headers, timeout=20
                        )
                    if resp.status_code == 200:
                        break
                    elif resp.status_code == 429:  # Too Many Requests
                        _interruptible_sleep(2 * (attempt + 1), stop_requested)
                        continue
                    else:
                        _interruptible_sleep(1 * (attempt + 1), stop_requested)
                        continue
                except (
                    HDEncodeRequestCancelled,
                    HDEncodeTrafficDenied,
                    _DetailRequestCancelled,
                ):
                    return None
                except Exception as e:
                    last_error = e
                    if hdencode_request:
                        get_hdencode_coordinator().observe_network_failure(
                            type(e).__name__
                        )
                    try:
                        _interruptible_sleep(1 * (attempt + 1), stop_requested)
                    except _DetailRequestCancelled:
                        return None

            if not resp or resp.status_code != 200:
                if self.app.config.get("debug_mode"):
                    self.app.safe_log(f"[Scrape Error] Failed after {max_retries} attempts: {last_error or 'Status ' + str(resp.status_code if resp else 'None')}")
                return None

            soup = BeautifulSoup(resp.content, 'html.parser', from_encoding='utf-8')

            # Narrow down text to content area to avoid sidebar/footer matches
            content_div = soup.find('div', class_='entry-content') or \
                          soup.find('div', class_='post-content') or \
                          soup.find('article') or \
                          soup.find('div', id='content') or \
                          soup
            text = content_div.get_text()

            fn_match = re.search(r'Filename\.+:[ \t]*(\S.*)', text) or re.search(r'Filename\.*:[ \t]*(\S.*)', text)
            if not fn_match:
                if content_div != soup:
                    text = soup.get_text()
                    fn_match = re.search(r'Filename\.+:[ \t]*(\S.*)', text) or re.search(r'Filename\.*:[ \t]*(\S.*)', text)

            if not fn_match:
                return None
            full_fn = fn_match.group(1).strip()

            # Count all episodes (number of Filename entries) for TV packs
            all_filenames = re.findall(r'Filename\.*:[ \t]*\S.*', text)
            episodes_count = len(all_filenames)

            # Smart Check: Scan ALL filenames for unique episode numbers
            # This distinguishes "Season Pack" (E01, E02...) from "Single Ep with Mirrors" (E01, E01...)
            unique_ep_nums = set()
            for fn_line in all_filenames:
                se_line = release_grammar.parse_season_episode(fn_line)
                if se_line.episode is not None:
                    last = se_line.episode_end or se_line.episode
                    unique_ep_nums.update(range(se_line.episode, last + 1))

            # Use unique episode count instead of total filenames (handles mirrors/duplicates)
            if unique_ep_nums:
                episodes_count = len(unique_ep_nums)

            # Season, episode and year now come from the ONE shared grammar
            # (R-3). DetailScraper was a third grammar with its own defects:
            # a two-digit season cap that read S104 as season 10, and a year
            # rule without the shared guards. The title still cuts where the
            # grammar says the metadata starts.
            is_tv = False
            season = None
            episode_number = None
            episode_end = None
            year = 0

            def _clean_cut(raw):
                # Shared title cleaner: dots/underscores to spaces, then strip
                # the separator punctuation the old regexes consumed -- the
                # review showed 'Movie Title (2020)' yielding 'Movie Title ('.
                return raw.replace('.', ' ').replace('_', ' ').strip(' -([')

            se = release_grammar.parse_season_episode(full_fn)
            if se.ambiguous:
                # Over-wide season token: "cannot tell", never a truncated
                # guess. No typed claim is made; the token still marks where
                # the title's metadata begins.
                clean_title = _clean_cut(full_fn[:se.start]) or full_fn
            elif se.season is not None:
                is_tv = True
                season = se.season
                episode_number = se.episode

                # Pack vs glued-range (round-13 semantics): a SEPARATE
                # filename line carrying an episode outside the primary
                # file's own parsed range means a Season Pack — no single
                # episode number describes it, and a contiguous range is
                # never invented from the file count. But a glued
                # multi-episode file (S01E01E03) or mirrored copies of the
                # same file keep their episode and carry the grammar's
                # parsed range end.
                if episode_number is not None:
                    own_range = set(range(se.episode, (se.episode_end or se.episode) + 1))
                    if unique_ep_nums and not unique_ep_nums.issubset(own_range):
                        if self.app.config.get("debug_mode"):
                            self.app.safe_log(f"[DEBUG] '{full_fn}' has {len(unique_ep_nums)} unique eps -> Treating as Season Pack")
                        episode_number = None
                    else:
                        episode_end = se.episode_end

                clean_title = _clean_cut(full_fn[:se.start]) or full_fn
            else:
                # Year-retry (round-10 rework): a year token that opens the
                # filename is part of the NAME ("2001 A Space Odyssey 1968");
                # instead of giving up, try the next token. First token whose
                # left side is a non-empty title wins.
                # Round-11 Finding 4: consult THE single year authority --
                # duplicating its rule here is how the deployed listing path
                # drifts behind the next selector change.
                year_match = release_grammar.select_release_year(full_fn)
                if year_match:
                    clean_title = _clean_cut(full_fn[:year_match.start]) or None
                    if clean_title:
                        year = year_match.year
                else:
                    clean_title = None
                if clean_title is None:
                    # No usable year: cut at the grammar's metadata boundary
                    # so a dimension or resolution token stays out of the
                    # title ('Concert.Film.1920x1080' -> 'Concert Film').
                    ms = release_grammar.metadata_start(full_fn)
                    clean_title = (_clean_cut(full_fn[:ms]) or full_fn) if ms < len(full_fn) else full_fn

            # Repair cp437 mojibake (e.g. ΓÇÖ → ') common on Windows-sourced filenames
            try:
                clean_title = clean_title.encode('cp437').decode('utf-8')
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass  # Not cp437 mojibake, keep original

            # Normalize smart quotes and dashes to ASCII
            clean_title = clean_title.replace('\u2019', "'").replace('\u2018', "'").replace('\u2014', '-').replace('\u2013', '-')

            rating_match = re.search(r'Rating\s*:\s*(\d+(\.\d+)?)', text, re.IGNORECASE)
            rating = rating_match.group(1) if rating_match else "-"

            # ROBUST SIZE FINDING: every size via the shared grammar (TB/TiB
            # included -- divergence (e) lived here in a third copy), keep the
            # labelled-size preference, pick the largest.
            all_sizes = release_grammar.find_all_sizes(text)
            labelled = [m for m in all_sizes
                        if re.search(r'(?<![a-z])size(?![a-z])', text[max(0, m.start - 24):m.start], re.IGNORECASE)]
            size_matches = labelled or all_sizes
            if not labelled and size_matches and self.app.config.get("debug_mode", False):
                self.app.safe_log(f"[DEBUG] Using loose size matches for '{clean_title}': {[m.text for m in size_matches]}")

            size = "?"
            if size_matches:
                max_gb = -1.0
                best_size = "?"
                found_sizes = []

                for s_match in size_matches:
                    gb_val = s_match.gigabytes
                    found_sizes.append(f"{s_match.text}({gb_val:.2f}GB)")
                    if gb_val > max_gb:
                        max_gb = gb_val
                        best_size = s_match.text.upper()

                size = best_size
                if self.app.config.get("debug_mode", False):
                    self.app.safe_log(f"[DEBUG] '{clean_title}' found sizes: {found_sizes} -> Selected: {size}")
            else:
                if self.app.config.get("debug_mode", False):
                    self.app.safe_log(f"[DEBUG] No size found for '{clean_title}' | Text Sample: {text[:200].replace(chr(10), ' ')}")
                    if 'size' in text.lower():
                        idx = text.lower().find('size')
                        snippet = text[max(0, idx-20):min(len(text), idx+50)].replace('\n', ' ')
                        self.app.safe_log(f"[DEBUG] 'Size' keyword found at {idx}: '...{snippet}...'")

            # Resolution tokens come from the shared grammar vocabulary; an
            # explicit WxH converts ONLY through the grammar's named dimension
            # bridge -- a dimension is never itself a resolution.
            _res_display = {"UHD": "4K", "1080P": "1080p", "720P": "720p"}
            res = "?"
            res_line = re.search(r'Resolution\.*:\s*([^\r\n]+)', text, re.IGNORECASE)
            if res_line:
                token = release_grammar.find_resolution(res_line.group(1))
                canon = token.canonical if token else release_grammar.resolution_from_dimensions(res_line.group(1))
                res = _res_display.get(canon, "?")

            # Prefer filename resolution if explicit
            fn_token = release_grammar.find_resolution(full_fn)
            fn_canon = fn_token.canonical if fn_token else release_grammar.resolution_from_dimensions(full_fn)
            if fn_canon in _res_display:
                res = _res_display[fn_canon]

            # (the pre-unification substring override block that survived the
            # first R-3 patch was removed here -- round-10 internal review,
            # executed proof: it reapplied the old behaviour after the new
            # grammar blocks and made declared delta 2 false.)

            hdr = "SDR"
            dovi = False
            if re.search(r'\b(DV|DoVi|Dolby\s?Vision)\b', full_fn, re.IGNORECASE):
                dovi = True
            # Codec evidence from THE shared HEVC vocabulary (the same regex
            # the feed-title parse asserts with), positive-only: a release
            # filename without the token is NOT thereby H.264.
            hevc = True if HEVC_TOKEN_RE.search(full_fn) else None
            hdr_match = re.search(r'Color primaries\.*:\s*(.+)', text, re.IGNORECASE)
            if hdr_match:
                ht = hdr_match.group(1).lower()
                if "bt.2020" in ht or "hdr" in ht:
                    hdr = "HDR"
                if "dovi" in ht or "dolby vision" in ht:
                    dovi = True

            full_text = soup.get_text()
            imdb_link = None
            imdb_id = None
            for a in soup.find_all('a', href=True):
                if "imdb.com/title/" in a['href']:
                    imdb_link = a['href']
                    id_match = re.search(r'(tt\d+)', imdb_link)
                    if id_match:
                        imdb_id = id_match.group(1)
                    break
            if not imdb_id:
                pt = re.search(r'(?:imdb\.com/title/|imdb[:\s]+)(tt\d{7,})', full_text, re.IGNORECASE)
                if pt:
                    imdb_id = pt.group(1)
                    imdb_link = f"https://www.imdb.com/title/{imdb_id}/"

            # Extract posted date (e.g. "Posted on March 1, 2026 at 03:15 PM")
            posted_date = None
            date_match = re.search(
                r'Posted\s+on\s+(\w+\s+\d{1,2},?\s+\d{4}\s+at\s+\d{1,2}:\d{2}\s*[AP]M)',
                full_text, re.IGNORECASE
            )
            if date_match:
                posted_date = date_match.group(1)

            # Extract multi-episode hints from page body (regex only — Ollama is async)
            try:
                hints = _llm.extract_page_hints(full_text)
                multi_episode_hint = hints if hints and (hints.get("is_combined") or hints.get("is_split")) else None
            except Exception:
                multi_episode_hint = None

            return {
                'display_title': clean_title,
                'year': year,
                'rating': rating,
                'search_key': self.app.clean_string(clean_title),
                'url': url,
                'imdb_link': imdb_link,
                'imdb_id': imdb_id,
                'size': size,
                'res': res,
                'hdr': hdr,
                'dovi': dovi,
                'tmdb_votes': "-",
                'is_tv': is_tv,
                'season': season,
                'episode_number': episode_number,
                'episode_end': episode_end,
                'episodes': episodes_count if is_tv else None,
                'hevc': hevc,
                'posted_date': posted_date,
                'multi_episode_hint': multi_episode_hint,
            }
        except Exception as e:
            if self.app.config.get("debug_mode", False):
                self.app.safe_log(f"Scrape Details Error ({url}): {e}")
            return None
