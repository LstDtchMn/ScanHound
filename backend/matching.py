"""Matching module - Handles Plex library matching and upgrade status calculation.

This module provides optimized matching between web-scraped items and Plex library items.
Matching uses a tiered approach for performance:
1. O(1) IMDb ID lookup (fastest, most reliable)
2. O(1) Exact title hash lookup
3. O(n) Fuzzy matching with pre-filtering (fallback)
"""

import logging
from functools import lru_cache
from typing import Dict, List, Tuple, Any, Optional, TypedDict
from thefuzz import fuzz

logger = logging.getLogger(__name__)


# Cached fuzzy matching functions for performance
# These cache the results of expensive string comparisons
@lru_cache(maxsize=10000)
def cached_fuzz_ratio(s1: str, s2: str) -> int:
    """Cached version of fuzz.ratio for repeated comparisons."""
    return fuzz.ratio(s1, s2)


@lru_cache(maxsize=10000)
def cached_token_sort_ratio(s1: str, s2: str) -> int:
    """Cached version of fuzz.token_sort_ratio for repeated comparisons."""
    return fuzz.token_sort_ratio(s1, s2)


def clear_fuzzy_cache():
    """Clear the fuzzy matching caches."""
    cached_fuzz_ratio.cache_clear()
    cached_token_sort_ratio.cache_clear()


def get_fuzzy_cache_info() -> Dict[str, Any]:
    """Get cache statistics for debugging."""
    ratio_info = cached_fuzz_ratio.cache_info()
    token_info = cached_token_sort_ratio.cache_info()
    return {
        'ratio_hits': ratio_info.hits,
        'ratio_misses': ratio_info.misses,
        'ratio_size': ratio_info.currsize,
        'token_hits': token_info.hits,
        'token_misses': token_info.misses,
        'token_size': token_info.currsize,
        'total_hits': ratio_info.hits + token_info.hits,
        'total_misses': ratio_info.misses + token_info.misses,
        'hit_rate': (ratio_info.hits + token_info.hits) /
                    max(1, ratio_info.hits + token_info.hits + ratio_info.misses + token_info.misses) * 100
    }


class WebItem(TypedDict, total=False):
    """Type definition for a web-scraped item."""
    display_title: str
    year: int
    res: str
    size: str
    dovi: bool
    hdr: str
    url: str
    imdb_id: Optional[str]
    tmdb_id: Optional[str]
    is_tv: bool
    season: Optional[int]
    episodes: Optional[int]
    search_key: str


class PlexItem(TypedDict, total=False):
    """Type definition for a Plex library item."""
    clean_title: str
    original_title: str
    year: int
    res: str
    size: float
    dovi: bool
    hdr: bool
    imdb_id: Optional[str]
    rating_key: str
    season: Optional[int]
    episode_count: Optional[int]


class PlexIndex(TypedDict):
    """Type definition for the Plex lookup index structure."""
    by_imdb: Dict[str, List[PlexItem]]
    by_title: Dict[str, List[PlexItem]]
    all_items: List[PlexItem]


# Matching configuration constants
TITLE_LENGTH_TOLERANCE = 8  # Max character difference for fuzzy matching consideration
QUICK_RATIO_THRESHOLD = 50   # Minimum quick ratio to proceed with full fuzzy matching


class MatchingEngine:
    """Handles matching web items against Plex library and calculating upgrade status.

    Requires a parent app object that provides:
        - config: dict with upgrade rules, thresholds, etc.
        - clean_string(s): normalize string for matching
        - parse_size(s): parse size string to GB float
        - safe_log(msg): log a message
        - download_history: set of downloaded URLs
        - STATUS_*, COLOR_*, EMOJI_*, RESOLUTION_ORDER: class-level constants
    """

    def __init__(self, parent_app):
        """Initialize matching engine with reference to parent app.

        Args:
            parent_app: Reference to main MovieScannerApp instance or compatible adapter
        """
        self.app = parent_app

    def check_codec_preference(self, web: WebItem) -> Tuple[bool, str]:
        """Check whether the web item satisfies an active codec/HDR preference.

        Reads ``pref_hevc`` and ``pref_hdr10plus`` from config. When a
        preference is enabled, the web item's display title is scanned for the
        corresponding codec marker (case-insensitive).

        Args:
            web: Web item dict — only ``display_title`` is inspected.

        Returns:
            (True, preference_label) when the item matches an active preference,
            (False, "") otherwise.  preference_label is e.g. "HEVC" or "HDR10+".
        """
        title = web.get('display_title', '').upper()
        
        # HEVC Preference
        if self.app.config.get("pref_hevc", False):
            if 'X265' in title or 'HEVC' in title or 'H.265' in title:
                return True, "HEVC"

        # HDR10+ Preference
        if self.app.config.get("pref_hdr10plus", False):
            if 'HDR10+' in title or 'HDR10PLUS' in title:
                return True, "HDR10+"
        
        return False, ""

    def find_tv_season_matches(
        self,
        web: WebItem,
        plex_index: PlexIndex
    ) -> Tuple[List[PlexItem], bool]:
        """Find Plex matches for a TV season.

        Args:
            web: Web item dict (must have is_tv=True and season set)
            plex_index: Pre-computed Plex lookup index

        Returns:
            tuple: (matches list, is_uncertain bool)
        """
        matches = []
        is_uncertain = False
        web_season = web.get('season')

        # Ensure search_key exists (legacy support)
        if 'search_key' not in web:
            web['search_key'] = self.app.clean_string(web['display_title']).lower()

        # 1. Match by IMDb ID + Season (O(1))
        if web.get('imdb_id') and web['imdb_id'] in plex_index["by_imdb"]:
            matches.extend([p for p in plex_index["by_imdb"][web['imdb_id']]
                           if p.get('season') == web_season])

        # 2. Match by Title (Fuzzy) + Season + Year
        if not matches:
            matches, is_uncertain = self.fuzzy_match_tv_season(web, plex_index, web_season)
            
        if self.app.config.get("debug_mode", False):
            self.app.safe_log(f"[DEBUG] matching.py: '{web['display_title']}' Matches={len(matches)}")

        return matches, is_uncertain

    def _get_word_candidates(
        self,
        search_key: str,
        plex_index: PlexIndex,
        pool: List[PlexItem],
    ) -> List[PlexItem]:
        """Narrow a candidate pool using the word index before fuzzy matching.

        Looks up each word in ``search_key`` (≥3 chars) against the pre-built
        ``by_word`` index and collects the union of matching Plex items that are
        also present in ``pool``.  Order of first encounter is preserved.

        This is a fast O(words × bucket_size) pre-filter that keeps the
        subsequent O(n) fuzzy scan focused on plausible matches rather than the
        entire library.

        Args:
            search_key:  Normalized query string (e.g. "the dark knight 2008").
            plex_index:  Full Plex lookup index; must contain a "by_word" sub-dict.
            pool:        Pre-filtered subset of all Plex items to search within
                         (e.g. only items matching the target season number).

        Returns:
            Subset of ``pool`` that share at least one significant word with
            ``search_key``.  Falls back to the full ``pool`` if the word index
            is empty or yields no matches (safety net for edge cases).
        """
        by_word = plex_index.get("by_word", {})
        if not by_word:
            return pool

        pool_ids = {id(p) for p in pool}
        seen_ids: set = set()
        candidates: List[PlexItem] = []
        for word in search_key.split():
            if len(word) >= 3 and word in by_word:
                for item in by_word[word]:
                    item_id = id(item)
                    if item_id in pool_ids and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        candidates.append(item)

        return candidates if candidates else pool

    def _fuzzy_match_core(
        self,
        web: Dict[str, Any],
        plex_index: PlexIndex,
        candidates: List[PlexItem],
        threshold: int,
        low_threshold: int = 0,
        year_tol: int = 1,
    ) -> Tuple[List[PlexItem], bool]:
        """Core fuzzy matching shared by TV and movie matchers.

        Uses a tiered approach: exact title hash → word-index narrowing →
        two-pass fuzzy scan. When ``low_threshold`` > 0, matches between
        low and high thresholds are accepted if years fall within
        ``year_tol``.

        Args:
            web: Web item with ``search_key`` set.
            plex_index: Pre-computed Plex lookup index.
            candidates: Pre-filtered Plex items to search.
            threshold: Primary match score threshold.
            low_threshold: Secondary (year-gated) threshold; 0 disables.
            year_tol: Allowed year difference for low-threshold matches.

        Returns:
            (matches, is_uncertain)
        """
        matches: List[PlexItem] = []
        is_uncertain = False
        web_year = (web.get('year') or 0)
        search_key = web['search_key']
        web_title_len = len(search_key)
        is_debug = self.app.config.get("debug_mode", False)

        # Narrow candidates via word index before expensive fuzzy scan
        narrowed = self._get_word_candidates(search_key, plex_index, candidates)

        for p in narrowed:
            plex_title = p.get('clean_title', '')
            plex_title_len = len(plex_title)

            # Skip if title lengths differ too much
            if abs(web_title_len - plex_title_len) > TITLE_LENGTH_TOLERANCE:
                continue

            # Two-pass: quick ratio first, then full token sort
            quick_score = cached_fuzz_ratio(search_key, plex_title)
            if quick_score < QUICK_RATIO_THRESHOLD:
                continue

            score = cached_token_sort_ratio(search_key, plex_title)
            score_orig = cached_token_sort_ratio(search_key, p.get('original_title', ''))
            best_score = max(score, score_orig)

            if is_debug and best_score > 60:
                self.app.safe_log(
                    f"[FUZZY] Score: {best_score} | Web: {web['display_title']} "
                    f"<-> Plex: {p.get('clean_title', '?')}")

            if best_score > threshold:
                matches.append(p)
                if not web.get('imdb_id'):
                    is_uncertain = True
            elif low_threshold and best_score > low_threshold:
                plex_year = (p.get('year') or 0)
                if web_year > 0 and plex_year > 0 and abs(web_year - plex_year) <= year_tol:
                    matches.append(p)
                    if not web.get('imdb_id'):
                        is_uncertain = True

        return matches, is_uncertain

    def fuzzy_match_tv_season(
        self,
        web: Dict[str, Any],
        plex_index: PlexIndex,
        web_season: Optional[int]
    ) -> Tuple[List[PlexItem], bool]:
        """Fuzzy-match a TV season against the Plex library.

        Tries an O(1) exact title lookup first, then falls back to the
        full fuzzy scan via ``_fuzzy_match_core``.  The candidate pool is
        pre-filtered to items whose season number equals ``web_season`` to
        avoid spurious cross-season matches.

        Thresholds and year tolerance are read from config
        (``tv_match_threshold``, ``low_match_threshold``, ``year_tolerance``).

        Args:
            web:        Web item dict with ``search_key`` set.
            plex_index: Pre-computed Plex lookup index.
            web_season: Target season number (may be None for ambiguous entries).

        Returns:
            (matches, is_uncertain) — see ``_fuzzy_match_core`` for semantics.
        """
        tv_thresh = self.app.config.get('tv_match_threshold', 90)
        low_thresh = self.app.config.get('low_match_threshold', 80)
        year_tol = self.app.config.get('year_tolerance', 1)

        # O(1) exact title match
        if web['search_key'] in plex_index["by_title"]:
            exact = [p for p in plex_index["by_title"][web['search_key']]
                     if p.get('season') == web_season]
            if exact:
                return exact, False

        # Fuzzy scan over season-filtered candidates
        candidates = [p for p in plex_index["all_items"]
                      if p.get('season') == web_season]
        return self._fuzzy_match_core(
            web, plex_index, candidates,
            threshold=tv_thresh, low_threshold=low_thresh, year_tol=year_tol,
        )

    def find_movie_matches(
        self,
        web: WebItem,
        plex_index: PlexIndex
    ) -> Tuple[List[PlexItem], bool]:
        """Find Plex matches for a movie.

        Args:
            web: Web item dict (must have is_tv=False)
            plex_index: Pre-computed Plex lookup index

        Returns:
            tuple: (matches list, is_uncertain bool)
        """
        matches = []
        is_uncertain = False

        # Ensure search_key exists (legacy support)
        if 'search_key' not in web:
            web['search_key'] = self.app.clean_string(web['display_title']).lower()

        # 1. Match by IMDb ID (O(1))
        imdb_id = web.get('imdb_id')
        if imdb_id and imdb_id in plex_index["by_imdb"]:
            matches.extend(plex_index["by_imdb"][imdb_id])
            logger.debug(f"[match] '{web['display_title']}' matched by IMDb {imdb_id} → {len(matches)} hit(s)")
        elif imdb_id:
            logger.debug(f"[match] '{web['display_title']}' IMDb {imdb_id} not in Plex index")
        else:
            logger.debug(f"[match] '{web['display_title']}' has no IMDb ID")

        # 2. Match by Title (Fuzzy) if ID match failed
        if not matches and (web.get('year') or 0) > 0:
            matches, is_uncertain = self.fuzzy_match_movie(web, plex_index)
            if matches:
                logger.debug(f"[match] '{web['display_title']}' fuzzy matched → {len(matches)} hit(s)")
            else:
                logger.debug(f"[match] '{web['display_title']}' (key='{web.get('search_key')}', year={web.get('year')}) NO MATCH in Plex")
        elif not matches:
            logger.debug(f"[match] '{web['display_title']}' year=0, skipping fuzzy match")

        return matches, is_uncertain

    def fuzzy_match_movie(
        self,
        web: Dict[str, Any],
        plex_index: PlexIndex
    ) -> Tuple[List[PlexItem], bool]:
        """Fuzzy-match a movie against the Plex library.

        Tries an O(1) exact title + year-tolerance lookup first, then falls
        back to ``_fuzzy_match_core``.  The candidate pool is pre-filtered by
        release year (± ``year_tolerance``) to reduce the fuzzy scan cost.

        Only called when ``find_movie_matches`` has a non-zero year for the
        web item; zero-year movies bypass fuzzy matching entirely to avoid
        false positives.

        Args:
            web:        Web item dict with ``search_key`` and ``year`` set.
            plex_index: Pre-computed Plex lookup index.

        Returns:
            (matches, is_uncertain) — see ``_fuzzy_match_core`` for semantics.
        """
        movie_thresh = self.app.config.get('movie_match_threshold', 85)
        year_tol = self.app.config.get('year_tolerance', 1)
        web_year = (web.get('year') or 0)

        # O(1) exact title match with year filter
        if web['search_key'] in plex_index["by_title"]:
            all_title_hits = plex_index["by_title"][web['search_key']]
            exact = [p for p in all_title_hits
                     if abs((p.get('year') or 0) - web_year) <= year_tol]
            if exact:
                logger.debug(f"[fuzzy] '{web['display_title']}' exact title hit: {len(exact)} match(es)")
                return exact, False
            else:
                logger.debug(f"[fuzzy] '{web['display_title']}' exact title found {len(all_title_hits)} item(s) but year filter rejected all "
                             f"(web_year={web_year}, plex_years={[p.get('year') for p in all_title_hits]}, tol={year_tol})")
        else:
            logger.debug(f"[fuzzy] '{web['display_title']}' key='{web['search_key']}' not in title index")

        # Fuzzy scan over year-filtered candidates
        candidates = [p for p in plex_index["all_items"]
                      if abs((p.get('year') or 0) - web_year) <= year_tol]
        logger.debug(f"[fuzzy] '{web['display_title']}' fuzzy scan: {len(candidates)} candidates (year {web_year}±{year_tol})")
        return self._fuzzy_match_core(
            web, plex_index, candidates,
            threshold=movie_thresh,
        )

    def check_download_history(self, web: WebItem) -> bool:
        """Check if item was already downloaded.

        Args:
            web: Web item to check

        Returns:
            True if already downloaded, False otherwise
        """
        return web['url'] in self.app.download_history

    def should_skip_by_preference(self, web: WebItem) -> bool:
        """Check if item should be skipped based on resolution preference.

        Args:
            web: Web item to check

        Returns:
            True if should skip based on user's resolution preference
        """
        pref = self.app.config.get("pref_res", "Prefer 4K")
        if pref == "Prefer 1080p" and web.get('res') == "4K":
            return True
        return False

    def log_match_debug_info(
        self,
        web: WebItem,
        matches: List[PlexItem],
        is_tv: bool
    ) -> None:
        """Log debug information about matches found.

        Args:
            web: Web item dict
            matches: List of Plex matches
            is_tv: Whether this is a TV item
        """
        if not self.app.config.get("debug_mode", False):
            return

        web_season = web.get('season')
        self.app.safe_log(f"[DEBUG] Comparing: {web['display_title']} ({web['year']})"
                          f"{f' S{web_season}' if is_tv else ''}")

        if matches:
            self.app.safe_log(f"   > Matches found: {len(matches)}")
            for m in matches:
                if is_tv:
                    self.app.safe_log(f"   > Local: {m.get('clean_title', '?')} "
                                      f"S{m.get('season', '?')} | Res: {m.get('res', '?')} | "
                                      f"Size: {m.get('size', 0)}GB | DV: {m.get('dovi', False)}")
                else:
                    self.app.safe_log(f"   > Local: {m.get('clean_title', '?')} | "
                                      f"Res: {m.get('res', '?')} | DV: {m.get('dovi', False)}")
        else:
            self.app.safe_log("   > No matches found in library.")

    def calculate_tv_upgrade_status(
        self,
        web: WebItem,
        match: PlexItem
    ) -> Tuple[str, str, str, bool]:
        """Calculate upgrade status for a TV season match.

        Args:
            web: Web item dict
            match: Single Plex match (first from matches list)

        Returns:
            Tuple of (status string, color hex, info string, is_upgrade flag)
        """
        plex_res = match.get('res', '?')
        plex_size = match.get('size', 0)
        plex_dovi = match.get('dovi', False)
        plex_hdr = match.get('hdr', False)
        plex_eps = match.get('episode_count', 0)

        web_res = web.get('res', '?')
        web_size = self.app.parse_size(web.get('size', '0'))
        sens = self.app.config.get("upgrade_sensitivity", 2) / 100.0

        # Build Plex info string
        dv_tag = f" {self.app.EMOJI_DV}" if plex_dovi else (" HDR" if plex_hdr else "")
        plex_res_disp = self.app.EMOJI_4K if plex_res == "4K" else plex_res

        episode_number = web.get('episode_number')

        if episode_number:
            # SINGLE EPISODE VS SEASON PACK Display
            # Don't show average size or size comparison as it's meaningless
            info = f"Have S{match.get('season', '?')} (Season Pack) [{plex_res_disp}]{dv_tag}"
        else:
            # SEASON VS SEASON Display
            avg_ep_size = round(plex_size / plex_eps, 2) if plex_eps > 0 else 0
            ep_size_str = f" ~{avg_ep_size}GB/ep" if plex_eps > 0 else ""

            info = f"Have S{match.get('season', '?')} [{plex_res_disp}] {plex_size}GB{ep_size_str}{dv_tag}"

            web_eps = web.get('episodes', 0)

            # Add episode count with mismatch warning
            if plex_eps and web_eps:
                if plex_eps != web_eps:
                    info += f" {self.app.EMOJI_WARNING} ({plex_eps}ep vs {web_eps}ep)"
                else:
                    info += f" ({plex_eps}ep)"
            elif plex_eps:
                info += f" ({plex_eps}ep)"

        is_upgrade = False
        status = self.app.STATUS_IN_LIBRARY_CHECK
        color = self.app.COLOR_IN_LIBRARY

        # 1. Resolution Upgrade (720p→1080p→4K)
        if self.app.RESOLUTION_ORDER.get(web_res, 0) > self.app.RESOLUTION_ORDER.get(plex_res, 0):
            is_upgrade = True
            if web_res == '4K':
                status = self.app.STATUS_UPGRADE_4K
            else:
                status = f"UPGRADE ({web_res})"
            color = self.app.COLOR_UPGRADE

        # 2. DV Upgrade
        elif web.get('dovi') and not plex_dovi:
            is_upgrade = True
            status = self.app.STATUS_DV_UPGRADE
            color = self.app.COLOR_DV_UPGRADE

        # 3. Size Upgrade (only if same resolution)
        elif web_res == plex_res and plex_size > 0:
            if web_size > plex_size * (1 + sens):
                is_upgrade = True
                diff = int(((web_size - plex_size) / plex_size) * 100) if plex_size > 0 else 0

                # Compare PER EPISODE if possible
                if plex_eps > 0 and web.get('episodes', 0) > 0:
                    p_avg = plex_size / plex_eps
                    w_avg = web_size / web.get('episodes', 1)
                    diff = int(((w_avg - p_avg) / p_avg) * 100) if p_avg > 0 else 0

                status = f"UPGRADE (+{diff}%)"
                color = self.app.COLOR_UPGRADE

        # 4. Codec/HDR Preference Upgrade
        if not is_upgrade and web_res == plex_res:
            is_pref, pref_type = self.check_codec_preference(web)
            if is_pref:
                # Allow size reduction for HEVC (efficiency)
                min_size_ratio = 0.6 if pref_type == "HEVC" else 1.0
                if web_size > (plex_size * min_size_ratio):
                    is_upgrade = True
                    status = f"UPGRADE ({pref_type})"
                    color = self.app.COLOR_UPGRADE

        logger.debug(
            "TV    '%s' S%s [web=%s %.1fGB] → %-20s | plex=%s %.1fGB%s",
            web.get('display_title', '?'), web.get('season', '?'),
            web_res, web_size,
            status,
            plex_res, plex_size,
            " DV" if plex_dovi else "",
        )
        return status, color, info, is_upgrade

    def calculate_movie_upgrade_status(
        self,
        web: WebItem,
        matches: List[PlexItem]
    ) -> Tuple[str, str, str, Optional[str]]:
        """Calculate upgrade status for movie matches.

        Args:
            web: Web item dict
            matches: List of Plex matches

        Returns:
            Tuple of (status string, color hex, info string, plex_item_id)
        """
        web_res = web.get('res', '?')

        # Sort all matches by resolution priority (4K > 1080p > 720p), then by DV, then by size
        res_priority = {'4K': 3, '1080p': 2, '720p': 1, '?': 0}
        all_sorted = sorted(
            matches,
            key=lambda x: (res_priority.get(x.get('res', '?'), 0), x.get('dovi', False), x.get('size', 0)),
            reverse=True
        )

        # Get highest resolution version (for comparison and linking)
        highest_res_match = all_sorted[0]

        # Find Best Resolution Match (same as web item)
        same_res = [m for m in matches if m.get('res') == web_res]
        if same_res:
            same_res.sort(key=lambda x: (x.get('dovi', False), x.get('size', 0)), reverse=True)
            exact = same_res[0]
            # Get secondary copy (different DV status or next largest size)
            secondary = None
            for m in same_res[1:]:
                if m.get('dovi') != exact.get('dovi') or m.get('size') != exact.get('size'):
                    secondary = m
                    break
        else:
            exact = None
            secondary = None

        # Always link to highest resolution version
        plex_item_id = highest_res_match['rating_key']

        # Max Local Size for comparisons (across ALL matches, not just same_res)
        max_local_size = max([x.get('size', 0) for x in matches]) if matches else 0
        # Max same-res size for more accurate comparisons
        max_same_res_size = max([x.get('size', 0) for x in same_res]) if same_res else max_local_size

        # Always compare against highest resolution version for upgrade logic
        if exact:
            status, color, info = self.calculate_movie_exact_match_status(
                web, exact, same_res, all_sorted, max_local_size, max_same_res_size
            )
        else:
            status, color, info = self.calculate_movie_fallback_status(web, all_sorted)

        logger.debug(
            "MOVIE '%s' (%s) [web=%s %.1fGB] → %-20s | %d Plex match(es) via %s"
            " | best plex=%s %.1fGB%s",
            web.get('display_title', '?'), web.get('year', '?'),
            web_res, self.app.parse_size(web.get('size', '0')),
            status,
            len(matches),
            "exact" if exact else "fallback",
            highest_res_match.get('res', '?'), highest_res_match.get('size', 0),
            " DV" if highest_res_match.get('dovi') else "",
        )
        return status, color, info, plex_item_id

    def calculate_movie_exact_match_status(
        self,
        web: WebItem,
        exact: PlexItem,
        same_res: List[PlexItem],
        matches: List[PlexItem],
        max_local_size: float,
        max_same_res_size: float
    ) -> Tuple[str, str, str]:
        """Calculate status when exact resolution match exists.

        This is a complex method that handles all the movie upgrade rules.
        Kept as a single method to preserve the sequential rule evaluation logic.

        Returns:
            Tuple of (status string, color hex, info string)
        """
        # Extract web item properties with safe defaults
        web_res = web.get('res', '?')
        web_dovi = web.get('dovi', False)

        p_size = exact.get('size', 0)
        w_size = self.app.parse_size(web.get('size', '0'))
        sens = self.app.config.get("upgrade_sensitivity", 2) / 100.0
        is_upgrade = False
        is_debug = self.app.config.get("debug_mode", False)

        # Define display tags early for use in logic
        dv_tag = f" {self.app.EMOJI_DV}" if exact.get('dovi') else (" HDR" if exact.get('hdr') else "")
        r_disp = self.app.EMOJI_4K if exact.get('res') == "4K" else exact.get('res', '?')

        info = f"{p_size}GB [{r_disp}]{dv_tag}"  # Default info to prevent UnboundLocalError
        status = self.app.STATUS_MISSING
        color = self.app.COLOR_MISSING

        # 0. STRICT RESOLUTION CHECK
        if is_debug:
            s_r = self.app.config.get("strict_resolution")
            self.app.safe_log(f"   > Strict Check: Web={web_res} Local={exact.get('res', '?')} Config={s_r}")

        if self.app.config.get("strict_resolution", False):
            if (exact.get('res') == '1080p' and web_res == '4K') or \
               (exact.get('res') == '4K' and web_res == '1080p'):
                status = self.app.STATUS_MISSING
                color = self.app.COLOR_MISSING

                # Show all available versions when strict matching finds resolution mismatch
                def get_version_spec(m):
                    sz = m.get('size', 0)
                    r = self.app.EMOJI_4K if m.get('res') == "4K" else m.get('res', '?')
                    dv = f" {self.app.EMOJI_DV}" if m.get('dovi') else (" HDR" if m.get('hdr') else "")
                    return f"{r}{dv} ({sz}GB)"

                # Group by resolution to show all versions
                versions_by_res = {}
                for m in matches:
                    res_key = m.get('res', '?')
                    if res_key not in versions_by_res:
                        versions_by_res[res_key] = []
                    versions_by_res[res_key].append(m)

                # Sort resolutions by priority and build info string
                res_priority = {'4K': 3, '1080p': 2, '720p': 1, '?': 0}
                sorted_res_keys = sorted(versions_by_res.keys(),
                                        key=lambda k: res_priority.get(k, 0),
                                        reverse=True)

                version_specs = []
                for res_key in sorted_res_keys:
                    # Get best version of this resolution (highest DV/size)
                    best = sorted(versions_by_res[res_key],
                                 key=lambda x: (x.get('dovi', False), x.get('size', 0)),
                                 reverse=True)[0]
                    version_specs.append(get_version_spec(best))

                info = "Have " + " + ".join(version_specs)
                is_upgrade = False
                if is_debug:
                    self.app.safe_log("   > Strict Mismatch: Forced MISSING.")
                logger.debug(
                    "  strict_res mismatch: web=%s plex=%s → MISSING",
                    web_res, exact.get('res', '?'),
                )

        # If strict check passed (didn't force MISSING), proceed to upgrade logic
        if status == self.app.STATUS_MISSING and not self.app.config.get("strict_resolution", False):
            status = self.app.STATUS_IN_LIBRARY  # Tentative default if not strict
        elif status == self.app.STATUS_MISSING and self.app.config.get("strict_resolution", False) and exact.get('res') == web_res:
            status = self.app.STATUS_IN_LIBRARY  # Strict mode passed (resolutions match)

        if status != self.app.STATUS_MISSING:
            # Case 1: Dolby Vision Upgrade
            if self.app.config.get("rule_dv", True) and web_dovi and not exact.get('dovi'):
                is_upgrade = True
                status = self.app.STATUS_DV_UPGRADE
                color = self.app.COLOR_DV_UPGRADE
                size_diff = ""
                if w_size > 0 and p_size > 0:
                    diff_pct = int(((w_size - p_size) / p_size) * 100)
                    size_diff = f", Web {'+' if diff_pct >= 0 else ''}{diff_pct}%"
                info = f"{p_size}GB (No {self.app.EMOJI_DV}{size_diff})"
                logger.debug("  rule_dv: DV upgrade (plex lacks DV, web=%.1fGB plex=%.1fGB)", w_size, p_size)

            # Case 2: 1080p -> 4K Upgrade
            elif self.app.config.get("rule_1080_4k", True) and exact.get('res') == '1080p' and web_res == '4K':
                if self.app.config.get("rule_1080_4k_size", False):
                    if w_size > max_local_size:
                        is_upgrade = True
                        diff = int(((w_size - max_local_size) / max_local_size) * 100) if max_local_size > 0 else 0
                        status = self.app.STATUS_UPGRADE_4K
                        color = self.app.COLOR_UPGRADE
                        info = f"{p_size}GB (Web +{diff}%)"
                        logger.debug("  rule_1080_4k (size-gated): web=%.1fGB > max_local=%.1fGB (+%d%%)", w_size, max_local_size, diff)
                    else:
                        logger.debug("  rule_1080_4k (size-gated): SKIPPED web=%.1fGB <= max_local=%.1fGB", w_size, max_local_size)
                else:
                    is_upgrade = True
                    status = self.app.STATUS_UPGRADE_4K
                    color = self.app.COLOR_UPGRADE
                    info = f"{p_size}GB (1080p)"
                    logger.debug("  rule_1080_4k: unconditional 4K upgrade")

            # Case 3: Same Resolution Size Upgrade (1080p)
            elif self.app.config.get("rule_1080_1080", True) and exact.get('res') == '1080p' and web_res == '1080p':
                if w_size > (p_size * (1 + sens)):
                    is_upgrade = True
                    diff = int(((w_size - p_size) / p_size) * 100) if p_size > 0 else 0
                    # Check if also a DV upgrade
                    if self.app.config.get("rule_dv", True) and web_dovi and not exact.get('dovi'):
                        status = self.app.STATUS_UPGRADE_SIZE_DV
                        color = self.app.COLOR_DV_UPGRADE
                        info = f"{p_size}GB (Web +{diff}%, +{self.app.EMOJI_DV})"
                    else:
                        status = self.app.STATUS_UPGRADE_SIZE
                        color = self.app.COLOR_UPGRADE
                        info = f"{p_size}GB (Web +{diff}%)"
                    logger.debug("  rule_1080_1080: size upgrade web=%.1fGB plex=%.1fGB (+%d%%)", w_size, p_size, diff)
                else:
                    logger.debug("  rule_1080_1080: SKIPPED web=%.1fGB <= plex=%.1fGB * (1+%.2f)", w_size, p_size, sens)

            # Case 4: Same Resolution Size Upgrade (4K)
            elif self.app.config.get("rule_4k_4k", True) and exact.get('res') == '4K' and web_res == '4K':
                if w_size > (p_size * (1 + sens)):
                    is_upgrade = True
                    diff = int(((w_size - p_size) / p_size) * 100) if p_size > 0 else 0
                    # Check if also a DV upgrade
                    if self.app.config.get("rule_dv", True) and web_dovi and not exact.get('dovi'):
                        status = self.app.STATUS_UPGRADE_SIZE_DV
                        color = self.app.COLOR_DV_UPGRADE
                        info = f"{p_size}GB [{r_disp}] (Web +{diff}%, +{self.app.EMOJI_DV})"
                    else:
                        status = self.app.STATUS_UPGRADE_SIZE
                        color = self.app.COLOR_UPGRADE
                        info = f"{p_size}GB [{r_disp}]{dv_tag} (Web +{diff}%)"
                    logger.debug("  rule_4k_4k: size upgrade web=%.1fGB plex=%.1fGB (+%d%%)", w_size, p_size, diff)
                else:
                    logger.debug("  rule_4k_4k: SKIPPED web=%.1fGB <= plex=%.1fGB * (1+%.2f)", w_size, p_size, sens)

            # Case 5: Codec/HDR Preference Upgrade
            if not is_upgrade and exact.get('res') == web_res:
                is_pref, pref_type = self.check_codec_preference(web)
                if is_pref:
                    # Allow size reduction for HEVC (efficiency)
                    min_size_ratio = 0.6 if pref_type == "HEVC" else 1.0
                    
                    if w_size > (p_size * min_size_ratio):
                        is_upgrade = True
                        status = f"UPGRADE ({pref_type})"
                        color = self.app.COLOR_UPGRADE
                        info = f"{p_size}GB [{r_disp}]{dv_tag} ({pref_type})"

            # Generic Function to build spec string (compact format)
            def get_spec_str(m, include_res=True, suffix=""):
                sz = m.get('size', 0)
                r = self.app.EMOJI_4K if m.get('res') == "4K" else m.get('res', '?')
                if m.get('dovi'):
                    hdr_tag = self.app.EMOJI_DV
                elif m.get('hdr') and m.get('hdr') not in ['SDR', '']:
                    hdr_tag = "HDR"
                else:
                    hdr_tag = ""

                if include_res:
                    return f"{sz}GB [{r}]{' ' + hdr_tag if hdr_tag else ''}{suffix}"
                else:
                    return f"{sz}GB{' ' + hdr_tag if hdr_tag else ''}{suffix}"

            # Extract comparison suffix from upgrade info (e.g. "(No DV, Web +0%)")
            comp_suffix = ""
            if is_upgrade and "(" in info:
                comp_suffix = " " + info[info.find("("):]

            # Build list of ALL local copies grouped by resolution
            # Sort: DV/HDR first, then size descending (use sorted() to avoid mutating caller's list)
            matches = sorted(matches, key=lambda x: (x.get('dovi', False), x.get('size', 0)), reverse=True)

            # Group by resolution for cleaner display (always show all versions)
            res_groups = {}
            for m in matches:
                res = m.get('res', '?')
                if res not in res_groups:
                    res_groups[res] = []
                res_groups[res].append(m)

            # Check if a match is the compared version (attach suffix to it)
            def is_compared(m):
                return m.get('size') == exact.get('size') and m.get('res') == exact.get('res') and m.get('dovi') == exact.get('dovi')

            # Build display string
            if len(res_groups) == 1:
                res = list(res_groups.keys())[0]
                r_disp = self.app.EMOJI_4K if res == "4K" else res
                copies = res_groups[res]
                if len(copies) == 1:
                    sfx = comp_suffix if is_upgrade else ""
                    specs_display = get_spec_str(copies[0], include_res=True, suffix=sfx)
                else:
                    copy_strs = [get_spec_str(m, include_res=False, suffix=comp_suffix if is_upgrade and is_compared(m) else "") for m in copies]
                    specs_display = f"[{r_disp}] " + " · ".join(copy_strs)
            else:
                local_specs = []
                for res in ['4K', '1080p', 'SD', '?']:
                    if res in res_groups:
                        for m in res_groups[res]:
                            sfx = comp_suffix if is_upgrade and is_compared(m) else ""
                            local_specs.append(get_spec_str(m, include_res=True, suffix=sfx))
                specs_display = " · ".join(local_specs)

            if is_upgrade:
                info = specs_display
            else:
                status = self.app.STATUS_IN_LIBRARY
                color = self.app.COLOR_IN_LIBRARY
                info = specs_display
                logger.debug("  no upgrade rule triggered → IN_LIBRARY")

        return status, color, info

    def calculate_movie_fallback_status(
        self,
        web: WebItem,
        matches: List[PlexItem]
    ) -> Tuple[str, str, str]:
        """Calculate status when no exact resolution match exists.

        Args:
            web: Web item dict
            matches: List of Plex matches

        Returns:
            Tuple of (status string, color hex, info string)
        """
        web_res = web.get('res', '?')

        res_list = list(set([c.get('res', '?') for c in matches]))
        pref = self.app.config.get("pref_res", "Prefer 4K")
        is_debug = self.app.config.get("debug_mode", False)
        max_local_size = max([x.get('size', 0) for x in matches]) if matches else 0

        # Build cleaner info display showing what we have
        def get_compact_spec(m):
            sz = m.get('size', 0)
            r = self.app.EMOJI_4K if m.get('res') == "4K" else m.get('res', '?')
            if m.get('dovi'):
                hdr_tag = self.app.EMOJI_DV
            elif m.get('hdr') and m.get('hdr') not in ['SDR', '']:
                hdr_tag = "HDR"
            else:
                hdr_tag = ""
            return f"{sz}GB [{r}]{' ' + hdr_tag if hdr_tag else ''}"

        # Sort and show best copies (sorted() avoids mutating caller's list)
        matches = sorted(matches, key=lambda x: (x.get('dovi', False), x.get('size', 0)), reverse=True)
        specs = [get_compact_spec(m) for m in matches[:2]]
        info = f"{self.app.EMOJI_INFO} Have " + " · ".join(specs)
        if len(matches) > 2:
            info += f" (+{len(matches) - 2})"

        # FALLBACK LOGIC
        if is_debug:
            s_r = self.app.config.get("strict_resolution")
            self.app.safe_log(f"   > Fallback Check: Pref='{pref}' Web='{web_res}' List={res_list} Strict={s_r}")

        strict = self.app.config.get("strict_resolution", False)

        # 1. Check if we already have the resolution (Safety Net)
        if web_res in res_list:
            status = self.app.STATUS_IN_LIBRARY
            color = self.app.COLOR_IN_LIBRARY
            logger.debug("FALLBACK  web=%s already in plex_res=%s → IN_LIBRARY", web_res, res_list)

        elif pref == "Prefer 4K" and web_res == "4K" and "4K" not in res_list:
            if strict:
                status = self.app.STATUS_MISSING
                color = self.app.COLOR_MISSING
                logger.debug("FALLBACK  Prefer4K web=4K strict=True plex_has=%s → MISSING", res_list)
            elif "1080p" in res_list and not self.app.config.get("rule_1080_4k", True):
                status = self.app.STATUS_MISSING
                color = self.app.COLOR_MISSING
                logger.debug("FALLBACK  Prefer4K web=4K rule_1080_4k=False → MISSING")
            else:
                status = self.app.STATUS_UPGRADE_4K
                color = self.app.COLOR_UPGRADE
                logger.debug("FALLBACK  Prefer4K web=4K plex_has=%s → UPGRADE_4K", res_list)

        elif pref == "No Preference":
            # STRICT MODE OVERRIDE: If strict mode is on and resolutions mismatch, it's MISSING
            if strict and web_res not in res_list:
                status = self.app.STATUS_MISSING
                color = self.app.COLOR_MISSING
                logger.debug("FALLBACK  NoPreference strict=True web=%s not in plex=%s → MISSING", web_res, res_list)
            else:
                w_size = self.app.parse_size(web.get('size', '0'))
                sens = self.app.config.get("upgrade_sensitivity", 2) / 100.0
                if w_size > (max_local_size * (1 + sens)):
                    status = self.app.STATUS_UPGRADE_SIZE
                    color = self.app.COLOR_UPGRADE
                    diff = int(((w_size - max_local_size) / max_local_size) * 100) if max_local_size > 0 else 0
                    info = f"{max_local_size}GB (Web +{diff}%)"
                    logger.debug("FALLBACK  NoPreference size upgrade web=%.1fGB max_local=%.1fGB (+%d%%)", w_size, max_local_size, diff)
                else:
                    status = self.app.STATUS_IN_LIBRARY
                    color = self.app.COLOR_IN_LIBRARY
                    logger.debug("FALLBACK  NoPreference no size trigger web=%.1fGB max_local=%.1fGB → IN_LIBRARY", w_size, max_local_size)
        else:
            status = self.app.STATUS_MISSING
            color = self.app.COLOR_MISSING
            logger.debug("FALLBACK  pref=%s web=%s plex=%s → MISSING (unmatched pref/res combo)", pref, web_res, res_list)

        return status, color, info
