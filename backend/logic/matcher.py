import re
import unicodedata
from typing import Dict, Any, List, Tuple, Optional
from thefuzz import fuzz

class PlexMatcher:
    """Handles logic for matching Web items to Plex library items.

    For full upgrade detection (DV, size, HDR, codec), set a MatchingEngine
    via set_matching_engine(). Without it, only basic 1080p->4K upgrades
    are detected.
    """

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config
        self.logger = logger
        self._matching_engine = None

    def set_matching_engine(self, engine):
        """Set the MatchingEngine for full upgrade detection.

        Args:
            engine: A matching.MatchingEngine instance
        """
        self._matching_engine = engine

    def clean_string(self, s: str) -> str:
        """Standardize string for comparison (matches ScannerController.clean_string)."""
        if not s: return ""
        s = str(s)
        s = unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('utf-8')
        s = s.lower()
        s = re.sub(r'[^\w\s]', '', s)
        return s.strip()

    def build_plex_lookup_index(self, plex_lib: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build optimized lookup index for Plex items.

        Includes word-based index for fast fuzzy candidate narrowing:
        ``by_word`` maps each word to the set of items containing it,
        allowing O(w) candidate lookup instead of O(n) linear scan.
        """
        index = {
            "by_imdb": {},
            "by_title": {},
            "by_title_year": {},
            "by_word": {},  # word -> list of items (for fuzzy pre-filter)
            "all_items": plex_lib
        }

        for item in plex_lib:
            # Index by IMDb ID
            if item.get('imdb_id'):
                if item['imdb_id'] not in index["by_imdb"]:
                    index["by_imdb"][item['imdb_id']] = []
                index["by_imdb"][item['imdb_id']].append(item)

            # Index by Title (Clean)
            if 'clean_title' not in item or not item['clean_title']:
                 item['clean_title'] = self.clean_string(item.get('title', ''))

            t = item.get('clean_title', '').lower()
            if t:
                if t not in index["by_title"]: index["by_title"][t] = []
                index["by_title"][t].append(item)

                # Index by Title + Year
                y = (item.get('year') or 0)
                if y > 0:
                    k = f"{t}|{y}"
                    if k not in index["by_title_year"]: index["by_title_year"][k] = []
                    index["by_title_year"][k].append(item)

                # Index by individual words (for fuzzy pre-filtering)
                for word in t.split():
                    if len(word) >= 3:  # Skip short words (a, the, of, etc.)
                        if word not in index["by_word"]:
                            index["by_word"][word] = []
                        index["by_word"][word].append(item)

        return index

    def compare_and_display(
        self,
        web: Dict[str, Any],
        plex_index: Dict[str, Any]
    ) -> Tuple[str, str, str, Optional[str], Optional[Dict[str, Any]]]:
        """Compare web item against Plex library using optimized index.
        
        Returns:
            tuple: (status, color, info, plex_id, match_item)
        """
        matches = []
        is_uncertain = False
        
        # Determine strictness
        is_tv = web.get('is_tv', False)
        
        if is_tv:
            matches, is_uncertain = self._find_tv_season_matches(web, plex_index)
        else:
            matches, is_uncertain = self._find_movie_matches(web, plex_index)
            
        # Decision Logic
        if not matches:
            return ("MISSING", "#ff4d4d", "Not in Library", None, None)
            
        # Match Found - Check for Upgrades
        match = matches[0] # Best match
        
        if is_tv:
            status, color, info, is_upgrade = self._calculate_tv_upgrade_status(web, match)
            return (status, color, info, match.get('rating_key'), match)
        else:
            status, color, info, plex_id = self._calculate_movie_upgrade_status(web, matches)
            return (status, color, info, plex_id, match)

    def _find_tv_season_matches(
        self,
        web: Dict[str, Any],
        plex_index: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Find Plex matches for a TV season."""
        matches = []
        is_uncertain = False
        web_season = web.get('season')

        if 'search_key' not in web:
            web['search_key'] = self.clean_string(web['display_title']).lower()

        # 1. Match by IMDb ID + Season (O(1))
        if web.get('imdb_id') and web['imdb_id'] in plex_index["by_imdb"]:
            matches.extend([p for p in plex_index["by_imdb"][web['imdb_id']]
                           if p.get('season') == web_season])

        # 2. Match by Title (Fuzzy) + Season + Year
        if not matches:
            matches, is_uncertain = self._fuzzy_match_tv_season(web, plex_index, web_season)

        return matches, is_uncertain

    def _get_word_candidates(self, search_key: str, plex_index: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get fuzzy match candidates using the word index.

        Returns items that share at least one significant word with the
        search key. Falls back to all_items if the word index yields
        nothing (safety net).
        """
        by_word = plex_index.get("by_word", {})
        if not by_word:
            return plex_index["all_items"]

        seen_ids = set()
        candidates = []
        for word in search_key.split():
            if len(word) >= 3 and word in by_word:
                for item in by_word[word]:
                    item_id = id(item)
                    if item_id not in seen_ids:
                        seen_ids.add(item_id)
                        candidates.append(item)

        return candidates if candidates else plex_index["all_items"]

    def _fuzzy_match_tv_season(
        self,
        web: Dict[str, Any],
        plex_index: Dict[str, Any],
        web_season: Optional[int]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        matches = []
        is_uncertain = False
        web_year = (web.get('year') or 0)
        tv_thresh = self.config.get('tv_match_threshold', 90)
        low_thresh = self.config.get('low_match_threshold', 80)
        year_tol = self.config.get('year_tolerance', 1)

        # Exact title match first
        if web['search_key'] in plex_index["by_title"]:
            exact_candidates = [p for p in plex_index["by_title"][web['search_key']]
                               if p.get('season') == web_season]
            matches.extend(exact_candidates)

        # Fallback to word-indexed fuzzy scan (narrowed from O(n) to O(candidates))
        if not matches:
            candidates = self._get_word_candidates(web['search_key'], plex_index)
            for p in candidates:
                if p.get('season') != web_season: continue

                score = fuzz.token_sort_ratio(web['search_key'], p.get('clean_title', ''))
                score_orig = fuzz.token_sort_ratio(web['search_key'], p.get('original_title', ''))
                best_score = max(score, score_orig)

                if best_score > tv_thresh:
                    matches.append(p)
                    if not web.get('imdb_id'): is_uncertain = True
                elif best_score > low_thresh:
                    plex_year = (p.get('year') or 0)
                    if web_year > 0 and plex_year > 0 and abs(web_year - plex_year) <= year_tol:
                        matches.append(p)
                        if not web.get('imdb_id'): is_uncertain = True

        return matches, is_uncertain

    def _find_movie_matches(
        self,
        web: Dict[str, Any],
        plex_index: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        matches = []
        is_uncertain = False

        if 'search_key' not in web:
            web['search_key'] = self.clean_string(web['display_title']).lower()

        # 1. Match by IMDb ID (O(1))
        if web.get('imdb_id') and web['imdb_id'] in plex_index["by_imdb"]:
            matches.extend(plex_index["by_imdb"][web['imdb_id']])

        # 2. Match by Title (Fuzzy)
        if not matches and (web.get('year') or 0) > 0:
            matches, is_uncertain = self._fuzzy_match_movie(web, plex_index)

        return matches, is_uncertain

    def _fuzzy_match_movie(
        self,
        web: Dict[str, Any],
        plex_index: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        matches = []
        is_uncertain = False
        movie_thresh = self.config.get('movie_match_threshold', 85)
        year_tol = self.config.get('year_tolerance', 1)
        web_year = (web.get('year') or 0)

        # Exact title match first
        if web['search_key'] in plex_index["by_title"]:
            for p in plex_index["by_title"][web['search_key']]:
                if abs((p.get('year') or 0) - web_year) <= year_tol:
                    matches.append(p)

        # Fallback to word-indexed fuzzy scan (narrowed candidates)
        if not matches:
            word_candidates = self._get_word_candidates(web['search_key'], plex_index)
            candidates = [p for p in word_candidates
                         if abs((p.get('year') or 0) - web_year) <= year_tol]

            matched_ids = set()
            for p in candidates:
                pid = id(p)
                if pid in matched_ids: continue
                score = fuzz.token_sort_ratio(web['search_key'], p.get('clean_title', ''))
                score_orig = fuzz.token_sort_ratio(web['search_key'], p.get('original_title', ''))

                if score > movie_thresh or score_orig > movie_thresh:
                    matches.append(p)
                    matched_ids.add(pid)
                    if not web.get('imdb_id'): is_uncertain = True

        return matches, is_uncertain

    def _calculate_tv_upgrade_status(
        self,
        web: Dict[str, Any],
        match: Dict[str, Any]
    ) -> Tuple[str, str, str, bool]:
        """Calculate upgrade status for a TV season match.

        Delegates to MatchingEngine if available for full DV/size/HDR detection.
        Falls back to basic 1080p->4K check otherwise.
        """
        if self._matching_engine:
            try:
                status, color, info, is_upgrade = self._matching_engine.calculate_tv_upgrade_status(web, match)
                return status, color, info, is_upgrade
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"MatchingEngine TV upgrade failed, using fallback: {e}")

        plex_res = match.get('res', '?')
        web_res = web.get('res', '?')
        is_upgrade = False

        status = "In Library"
        color = "#2ecc71"
        info = f"Have: {plex_res}"

        pref = self.config.get("pref_res", "Prefer 4K")
        rule_1080_4k = self.config.get("rule_1080_4k", True)

        if "4K" in web_res and "1080" in plex_res and pref == "Prefer 4K" and rule_1080_4k:
            status = "UPGRADE (4K)"
            color = "#f39c12"
            is_upgrade = True

        return status, color, info, is_upgrade

    def _calculate_movie_upgrade_status(
        self,
        web: Dict[str, Any],
        matches: List[Dict[str, Any]]
    ) -> Tuple[str, str, str, Optional[str]]:
        """Calculate upgrade status for movie matches.

        Delegates to MatchingEngine if available for full DV/size/HDR/codec detection.
        Falls back to basic 1080p->4K check otherwise.
        """
        match = matches[0]

        if self._matching_engine:
            try:
                status, color, info, plex_id = self._matching_engine.calculate_movie_upgrade_status(web, matches)
                return status, color, info, plex_id
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"MatchingEngine movie upgrade failed, using fallback: {e}")

        plex_res = match.get('res', '?')
        web_res = web.get('res', '?')

        status = "In Library"
        color = "#2ecc71"
        info = f"Have: {plex_res}"

        pref = self.config.get("pref_res", "Prefer 4K")
        rule_1080_4k = self.config.get("rule_1080_4k", True)

        if "4K" in web_res and "1080" in plex_res and pref == "Prefer 4K" and rule_1080_4k:
            status = "UPGRADE (4K)"
            color = "#f39c12"

        return status, color, info, match.get('rating_key')
