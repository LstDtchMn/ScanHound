"""PlexService — Plex library connection, loading, and indexing.

Handles connecting to a Plex server, loading movie and TV libraries,
building an in-memory index for fast matching, and managing the
local DB cache.  Framework-agnostic: communicates via callbacks.
"""

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from backend.app_service import clean_string as _clean_string
from backend.plex_manager import PlexManager
from backend.database import DatabaseManager

logger = logging.getLogger(__name__)

try:
    from plexapi.server import PlexServer
    PLEX_AVAILABLE = True
except ImportError:
    PLEX_AVAILABLE = False


class PlexService:
    """Manages Plex connection, library loading, caching, and index building."""

    def __init__(self, config: Dict[str, Any], db: DatabaseManager, plex_manager: PlexManager):
        self.config = config
        self.db = db
        self.plex_manager = plex_manager

        # Library data
        self.plex_movies: List[Dict] = []
        self.plex_tv: List[Dict] = []
        self.plex_index: Dict[str, Any] = {"by_imdb": {}, "by_title": {}, "all_items": []}
        self.stats: Dict[str, int] = {"plex_1080": 0, "plex_4k": 0, "tv_seasons": 0, "new_items": 0}

        # Loading state
        self._plex_loading = False
        self._plex_loading_lock = threading.Lock()
        self._last_full_load_time: float = 0  # unix timestamp of last full Plex API load

        # ── Completeness of the in-memory authority (public) ──────────
        # plex_movies/plex_tv/plex_index are what the scanner matches against,
        # so a caller has to be able to ask whether they cover every configured
        # library. Re-armed at the top of every load; False until one finishes.
        #: True when the loaded authority covers every configured library.
        self.last_load_complete: bool = False
        #: Content types ("Movies" / "TV Shows") whose live read was incomplete.
        self.last_load_incomplete_types: List[str] = []
        #: Libraries whose cached rows were merged back in because their live
        #: read failed or degraded.
        self.last_load_restored_libraries: List[str] = []
        #: How many cached rows that merge contributed.
        self.last_load_restored_rows: int = 0

        # Callbacks
        self._log_fn: Optional[Callable[[str, str], None]] = None
        self._stats_callback: Optional[Callable[[Dict[str, int]], None]] = None

    # ── Callbacks ─────────────────────────────────────────────────────

    def set_log_callback(self, fn: Callable[[str, str], None]):
        """Register a function to receive log messages (msg, level)."""
        self._log_fn = fn

    def set_stats_callback(self, fn: Callable[[Dict[str, int]], None]):
        """Register a function to receive Plex library statistics updates."""
        self._stats_callback = fn

    def _log(self, msg: str, level: str = "info"):
        """Emit a log message to both Python logging and the UI callback."""
        getattr(logger, level if level != "success" else "info", logger.info)(msg)
        if self._log_fn:
            try:
                self._log_fn(msg, level)
            except Exception:
                pass

    def _emit_stats(self):
        """Push current Plex stats to the registered UI callback."""
        if self._stats_callback:
            try:
                self._stats_callback(dict(self.stats))
            except Exception:
                pass

    # ── Connection ────────────────────────────────────────────────────

    def connect(self) -> tuple[bool, str]:
        """Connect to Plex server using current config. Returns (success, message)."""
        conn_mode = self.config.get("plex_connection_mode", "direct")
        plex_url = self.config.get("plex_url", "").strip()
        plex_token = self.config.get("plex_token", "").strip()
        plex_user = self.config.get("plex_username", "").strip()
        plex_pass = self.config.get("plex_password", "").strip()
        plex_srv = self.config.get("plex_server_name", "").strip()

        if conn_mode == "account":
            if not plex_user or not plex_pass:
                return False, "Plex username/password not configured"
        else:
            if not plex_url or not plex_token:
                return False, "Plex URL/token not configured"

        self._log(f"Connecting to Plex ({conn_mode})...")
        self.plex_manager.configure(
            plex_url, plex_token,
            connection_mode=conn_mode,
            username=plex_user, password=plex_pass,
            server_name=plex_srv,
        )
        success, msg = self.plex_manager.connect(timeout=30)

        if success:
            server_info = self.plex_manager.get_server_info()
            plex_server_id = server_info.get('machine_id', '') if server_info else ""
            plex_server_name = server_info.get('name', '') if server_info else ""
            self.config["plex_server_id"] = plex_server_id
            if plex_server_name:
                self.config["plex_server_name"] = plex_server_name
            elif conn_mode == "account" and self.plex_manager._server_name:
                self.config["plex_server_name"] = self.plex_manager._server_name
            self._log(msg, "success")
        else:
            self._log(f"Plex connection failed: {msg}", "error")

        return success, msg

    # ── Library loading ───────────────────────────────────────────────

    def _configured_libs(self) -> tuple[List[str], List[str]]:
        """Resolve the configured (movie_libs, tv_libs) library names.

        Use `or` so an explicit empty list falls through to the next fallback.
        Priority: movie_libs (user-assigned) → known_movie_libraries (legacy key).
        """
        movie_libs = (
            self.config.get("movie_libs")
            or self.config.get("known_movie_libraries")
            or []
        )
        tv_libs = (
            self.config.get("tv_libs")
            or self.config.get("known_tv_libraries")
            or []
        )
        return movie_libs, tv_libs

    def load_libraries(
        self,
        wait_if_loading: bool = False,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        use_cache: bool = False,
    ):
        """Load Plex library data and build index.

        Args:
            wait_if_loading: Wait for an in-progress load instead of skipping.
            progress_callback: Optional (0.0–1.0, message) callback.
            use_cache: Attempt to load from local DB cache first.
        """
        if not self.plex_manager.is_connected:
            return

        if self._plex_loading:
            if wait_if_loading:
                self._log("Waiting for Plex library load to complete...")
                with self._plex_loading_lock:
                    pass
                return
            else:
                self._log("Plex library load already in progress, skipping...")
                return

        if not self._plex_loading_lock.acquire(blocking=False):
            if wait_if_loading:
                self._log("Waiting for Plex library load to complete...")
                with self._plex_loading_lock:
                    pass
            return

        try:
            self._plex_loading = True

            # Re-armed per load so a caller inspecting these afterwards can
            # never read the PREVIOUS run's verdict as this run's.
            self.last_load_complete = False
            self.last_load_incomplete_types = []
            self.last_load_restored_libraries = []
            self.last_load_restored_rows = 0

            movie_libs, tv_libs = self._configured_libs()

            # ── Cache path ────────────────────────────────────────────
            if use_cache:
                self._log("Loading Plex data from local cache...")
                cached_movies = self.db.load_plex_cache("Movies")
                cached_tv = self.db.load_plex_cache("TV Shows")

                # A cache covering only SOME of the configured content types is
                # not authoritative. Returning it would leave the missing type
                # indexed with zero items, so every title in it reports Missing
                # — and rematch_cache's all-or-nothing `have_plex` guard then
                # writes that downgrade into the background cache. Fall through
                # to a full load instead (SH-H13).
                missing_types = []
                if movie_libs and not cached_movies:
                    missing_types.append("Movies")
                if tv_libs and not cached_tv:
                    missing_types.append("TV Shows")

                if cached_movies or cached_tv:
                    if missing_types:
                        self._log(
                            f"Plex cache has no {' or '.join(missing_types)} rows but those "
                            "libraries are configured — falling back to a full load",
                            "warning",
                        )
                    else:
                        self.plex_movies = cached_movies
                        self.plex_tv = cached_tv
                        self.stats['plex_4k'] = len({m.get('imdb_id') or m.get('clean_title', '') for m in self.plex_movies if m.get('res') == '4K'} - {''})
                        self.stats['plex_1080'] = len({m.get('imdb_id') or m.get('clean_title', '') for m in self.plex_movies if m.get('res') == '1080p'} - {''})
                        self.stats['tv_seasons'] = len(self.plex_tv)
                        self._build_plex_index()
                        self._emit_stats()
                        # Reached only when every CONFIGURED content type had
                        # cached rows (the missing_types gate above), so this
                        # authority is complete even though no library was read.
                        self.last_load_complete = True
                        self._last_full_load_time = time.time()
                        self._log(
                            f"Loaded Cache: {len(self.plex_movies)} movies, {self.stats['tv_seasons']} seasons",
                            "success",
                        )
                        return
                else:
                    self._log("Cache empty, falling back to full load...", "warning")

            # ── Full load ─────────────────────────────────────────────
            if movie_libs != self.config.get("movie_libs"):
                logger.warning(
                    "movie_libs is empty — falling back to known_movie_libraries: %s",
                    movie_libs,
                )
            if tv_libs != self.config.get("tv_libs"):
                logger.warning(
                    "tv_libs is empty — falling back to known_tv_libraries: %s",
                    tv_libs,
                )

            if not movie_libs and not tv_libs:
                self._log(
                    "No Plex libraries are configured. Go to Settings > Plex, click "
                    "'Test Connection', assign your libraries, and save.",
                    "error",
                )
                return

            _movies: List[Dict] = []
            _tv: List[Dict] = []
            tv_seasons = 0
            seen_movies: Set[int] = set()
            seen_shows: Set[int] = set()
            # Track unique movies per resolution by IMDb ID / title
            # to avoid double-counting across libraries
            seen_4k: Set[str] = set()
            seen_1080: Set[str] = set()

            total_libs = len(movie_libs) + len(tv_libs)
            current_lib_idx = 0
            # Set when a library-level load is interrupted partway through
            # (e.g. a Plex connection drop mid-iteration). The per-library
            # except below already swallows the error and moves on so the
            # overall load can proceed with the other libraries, but the
            # resulting _movies/_tv list for that content type is now known
            # incomplete — it must never full-replace a good existing cache.
            movies_load_incomplete = False
            tv_load_incomplete = False
            # Item-level extraction failures (as opposed to a whole-library
            # exception) also make the resulting list incomplete — an item
            # that legitimately had media data but failed to extract must
            # not be treated as "successfully absent" (SH-H07).
            movie_extract_fail = 0
            # WHICH libraries the flags above are about. The booleans are enough
            # to block a destructive full_replace, but not to repair the live
            # authority: _build_plex_index turns _movies/_tv into the matcher's
            # only view of the library, so a library that was skipped is a
            # library whose owned titles now read Missing. Recorded per library
            # so cached rows can be merged back for exactly those, leaving the
            # libraries that DID load on their fresh data (SH-P1).
            unreliable_movie_libs: Set[str] = set()
            unreliable_tv_libs: Set[str] = set()

            # ── Movies ────────────────────────────────────────────────
            for lib_name in movie_libs:
                if progress_callback:
                    progress_callback(current_lib_idx / total_libs, f"Loading {lib_name}...")

                try:
                    lib = self.plex_manager.get_library_section(lib_name)
                    if not lib:
                        # get_library_section swallows EVERY exception and
                        # returns None (timeout, NotFound after a Plex-side
                        # rename, auth blip), so "not found" is
                        # indistinguishable from "could not be read" — and
                        # either way this library's items are absent from
                        # _movies. Mark the content type incomplete so the
                        # full_replace gate below cannot prune every cached
                        # row this library owns (SH-H12).
                        self._log(f"Movie library '{lib_name}' not found", "warning")
                        movies_load_incomplete = True
                        unreliable_movie_libs.add(lib_name)
                        continue

                    items = lib.all()
                    total_items = len(items)
                    if total_items == 0:
                        self._log(f"Movie library '{lib_name}' returned 0 items — may be a Plex connection issue", "warning")

                    for i, movie in enumerate(items):
                        if progress_callback and i % 20 == 0 and total_items > 0:
                            lib_progress = (i / total_items) / total_libs
                            overall = (current_lib_idx / total_libs) + lib_progress
                            progress_callback(overall, f"Loading {lib_name} {i}/{total_items}")

                        if movie.ratingKey in seen_movies:
                            continue

                        movie_versions = self._extract_movie_data(movie)
                        if movie_versions:
                            for mv in movie_versions:
                                mv['library_name'] = lib_name
                            _movies.extend(movie_versions)
                            seen_movies.add(movie.ratingKey)
                            # Count unique movies per resolution using
                            # IMDb ID (or title fallback) to avoid
                            # double-counting across multiple libraries.
                            for mv in movie_versions:
                                uid = mv.get('imdb_id') or mv.get('clean_title', '')
                                if not uid:
                                    continue
                                if mv.get('res') == '4K':
                                    seen_4k.add(uid)
                                elif mv.get('res') == '1080p':
                                    seen_1080.add(uid)
                        elif movie.media:
                            # Had media data but extraction returned None —
                            # a real per-item failure, not a legitimately
                            # media-less item.
                            movie_extract_fail += 1
                            # Attributed HERE, not at the post-loop
                            # `if movie_extract_fail:` — the counter is shared
                            # across libraries, so by then lib_name is whichever
                            # library happened to be iterated last.
                            unreliable_movie_libs.add(lib_name)
                except Exception as e:
                    self._log(f"Error loading movie library '{lib_name}': {e}", "error")
                    movies_load_incomplete = True
                    unreliable_movie_libs.add(lib_name)

                current_lib_idx += 1

            if movie_extract_fail:
                self._log(f"{movie_extract_fail} movie item(s) failed extraction", "warning")
                movies_load_incomplete = True

            # ── TV Shows ──────────────────────────────────────────────
            for lib_name in tv_libs:
                if progress_callback:
                    progress_callback(current_lib_idx / total_libs, f"Loading {lib_name}...")

                try:
                    lib = self.plex_manager.get_library_section(lib_name)
                    if not lib:
                        # Same as the movie branch above: a None section is a
                        # swallowed error as often as a genuine absence, so the
                        # TV list is now known incomplete and must not
                        # full-replace the cache (SH-H12).
                        self._log(f"TV library '{lib_name}' not found", "warning")
                        tv_load_incomplete = True
                        unreliable_tv_libs.add(lib_name)
                        continue

                    items = lib.all()
                    total_items = len(items)
                    lib_type = getattr(lib, 'type', 'unknown')
                    self._log(f"Loading {total_items} items from '{lib_name}' (type={lib_type})...")

                    if lib_type != 'show':
                        self._log(
                            f"Library '{lib_name}' is type '{lib_type}', not 'show'. "
                            "Check Settings > Library Assignment.",
                            "error",
                        )

                    tv_errors = tv_no_seasons = tv_all_specials = tv_extract_fail = 0
                    first_error = None

                    for i, show in enumerate(items):
                        if progress_callback and i % 5 == 0:
                            lib_progress = (i / total_items) / total_libs
                            overall = (current_lib_idx / total_libs) + lib_progress
                            progress_callback(overall, f"Loading {lib_name} {i}/{total_items}")

                        try:
                            seasons_list = show.seasons()
                            if not seasons_list:
                                tv_no_seasons += 1
                                continue

                            show_all_specials = True
                            for season in seasons_list:
                                if season.index == 0:
                                    continue
                                show_all_specials = False
                                if season.ratingKey in seen_shows:
                                    continue
                                season_data = self._extract_season_data(show, season)
                                if season_data:
                                    season_data['library_name'] = lib_name
                                    _tv.append(season_data)
                                    seen_shows.add(season.ratingKey)
                                    tv_seasons += 1
                                else:
                                    tv_extract_fail += 1

                            if show_all_specials and len(seasons_list) > 0:
                                tv_all_specials += 1
                        except Exception as e:
                            tv_errors += 1
                            if first_error is None:
                                first_error = f"{show.title}: {type(e).__name__}: {e}"
                            logger.debug(f"Error loading show '{show.title}': {e}")

                    # Diagnostics. "only specials" is NORMAL (a show with just a
                    # season-0/specials season) — it is not a load problem, so it
                    # doesn't belong in the warning list (it fired 83x/scan). It's
                    # surfaced as informational context on the success line instead.
                    diag = []
                    if tv_errors:
                        diag.append(f"{tv_errors} errored")
                    if tv_no_seasons:
                        diag.append(f"{tv_no_seasons} had no seasons")
                    if tv_extract_fail:
                        diag.append(f"{tv_extract_fail} season extracts failed")
                    specials_note = f" ({tv_all_specials} specials-only)" if tv_all_specials else ""

                    if diag:
                        self._log(
                            f"TV loading: {total_items} shows, {tv_seasons} seasons{specials_note}. "
                            f"Issues: {', '.join(diag)}."
                            + (f" First error: {first_error}" if first_error else ""),
                            "warning" if tv_seasons > 0 else "error",
                        )
                    else:
                        self._log(f"Loaded {tv_seasons} seasons from {total_items} shows in '{lib_name}'{specials_note}", "success")

                    # A per-show or per-season extraction failure makes this
                    # content type's list incomplete, same as a whole-library
                    # exception — the cache-save gate below must not
                    # full_replace with a set that's missing owned items
                    # (SH-H07).
                    if tv_errors or tv_extract_fail:
                        tv_load_incomplete = True
                        unreliable_tv_libs.add(lib_name)

                except Exception as e:
                    self._log(f"Error loading TV library '{lib_name}': {e}", "error")
                    tv_load_incomplete = True
                    unreliable_tv_libs.add(lib_name)

                current_lib_idx += 1

            if tv_libs and tv_seasons == 0:
                self._log(
                    f"No TV seasons loaded from Plex (libraries: {', '.join(tv_libs)}). "
                    "TV shows will not match. Verify library names in Settings.",
                    "warning",
                )

            self.stats['plex_1080'] = len(seen_1080)
            self.stats['plex_4k'] = len(seen_4k)
            self.stats['tv_seasons'] = tv_seasons

            # Repair the live authority before it becomes the matcher's view of
            # the library. Protecting the cached rows on disk (the full_replace
            # gate below) is only half the job: those rows also have to be IN
            # the index, or the scanner reports owned titles as Missing and
            # auto-grab re-downloads them (SH-P1).
            movies_authority, movies_restored, movies_restored_libs = (
                self._restore_cached_libraries(_movies, "Movies", unreliable_movie_libs))
            tv_authority, tv_restored, tv_restored_libs = (
                self._restore_cached_libraries(_tv, "TV Shows", unreliable_tv_libs))

            # LAST RESORT when the cache could not cover a failed library.
            #
            # _restore_cached_libraries returns the live rows unchanged when the
            # cache read raises OR comes back empty — and load_plex_cache
            # swallows every error and returns [] — so "restored 0 rows for a
            # library that failed" means the repair above did nothing. Installing
            # the partial list then replaces a COMPLETE in-memory authority with
            # an incomplete one, which is the harm the finding names: measured
            # end to end, an owned 4K title read MISSING and auto-grab took it.
            #
            # Keeping the previous list is strictly better than a known-partial
            # one. It is stale by at most one cycle, whereas the partial list is
            # WRONG about titles the user owns right now. Doing nothing is the
            # only option that cannot cause a re-download.
            def _rows_per_library(rows):
                """Row count per library. Untagged legacy rows share one bucket.

                library_name is only on rows written since that column existed,
                so an older cache has none. Counting them together is enough for
                the comparison below, which only asks "did this library lose
                rows", never which row went where.
                """
                counts = {}
                for r in rows or ():
                    key = r.get('library_name') or '<untagged>'
                    counts[key] = counts.get(key, 0) + 1
                return counts

            def _keep_previous(live, restored_libs, unreliable, previous, label):
                # Round-3 review: the predicate was `restored or not previous`,
                # so ANY positive restored count blocked the fallback. That does
                # not prove every unreadable library was covered. Their case:
                # libraries A and B both unreadable, cache holds A only,
                # restored == 1, fallback rejected, and every title in B
                # disappears from the matcher and becomes auto-grabbable.
                #
                # Coverage is now checked PER LIBRARY, and against the previous
                # complete list rather than against a bare count -- which also
                # catches partial coverage WITHIN one library (the cache holding
                # some of A's rows but not all), and the mixed tagged/untagged
                # cache shape where restoring one tagged row would otherwise
                # mask untagged owned rows vanishing.
                if not unreliable or not previous:
                    return live

                uncovered = sorted(set(unreliable) - set(restored_libs or ()))
                prev_counts = _rows_per_library(previous)
                live_counts = _rows_per_library(live)
                shrunk = sorted(
                    lib for lib, n in prev_counts.items()
                    if live_counts.get(lib, 0) < n)

                if not uncovered and not shrunk:
                    return live
                self._log(
                    f"{label}: {len(unreliable)} library(ies) unreadable and the "
                    "cache did not fully cover them"
                    + (f" (no rows restored for: {', '.join(uncovered)})"
                       if uncovered else "")
                    + (f" (fewer rows than before for: {', '.join(shrunk)})"
                       if shrunk else "")
                    + f". Keeping the previous complete list of {len(previous)} "
                    "rather than replacing it with a partial one — owned titles "
                    "would otherwise read as Missing and be re-downloaded.",
                    "warning",
                )
                return list(previous)

            movies_authority = _keep_previous(
                movies_authority, movies_restored_libs, unreliable_movie_libs,
                self.plex_movies, "Movies")
            tv_authority = _keep_previous(
                tv_authority, tv_restored_libs, unreliable_tv_libs,
                self.plex_tv, "TV Shows")

            # Atomic swap — UI reads see complete state, never partial
            self.plex_movies = movies_authority
            self.plex_tv = tv_authority

            self._build_plex_index()

            self.last_load_restored_libraries = movies_restored_libs + tv_restored_libs
            self.last_load_restored_rows = movies_restored + tv_restored
            if self.last_load_restored_rows:
                self._log(
                    f"Restored {self.last_load_restored_rows} cached row(s) for "
                    f"{', '.join(self.last_load_restored_libraries)} so their titles "
                    "keep matching while those libraries are unreadable",
                    "warning",
                )

            # The cache-save decisions below deliberately read the LIVE lists
            # (_movies / _tv), not the merged authority above. Merging cached
            # rows back in must not make a partial load look complete enough to
            # full_replace, and must not re-save rows that are already in the
            # cache they came from.
            if not _movies and not _tv:
                self._log(
                    "Plex load returned 0 movies and 0 TV seasons — check library names in "
                    "Settings > Plex. Preserving existing cache.",
                    "warning",
                )
            else:
                # Persist to cache. Only full-replace a content type when its
                # load actually returned data AND completed without a
                # library-level exception — an empty list almost always
                # means a partial/failed load (e.g. one library's API call
                # failed), and a mid-library connection drop leaves a
                # non-empty but incomplete list; full_replace would otherwise
                # wipe a good cache with either.
                self._log("Saving to local cache...")
                if _movies and not movies_load_incomplete:
                    self.db.save_plex_cache(_movies, "Movies", full_replace=True)
                elif not _movies:
                    self._log("Skipping Movies cache save — load returned 0 (preserving existing cache)", "warning")
                else:
                    self._log(
                        "Movies load was interrupted (library error) — skipping full-replace "
                        "cache save to avoid clobbering good cache with a partial set",
                        "warning",
                    )
                if _tv and not tv_load_incomplete:
                    self.db.save_plex_cache(_tv, "TV Shows", full_replace=True)
                elif not _tv:
                    self._log("Skipping TV Shows cache save — load returned 0 (preserving existing cache)", "warning")
                else:
                    self._log(
                        "TV load was interrupted (library error) — skipping full-replace "
                        "cache save to avoid clobbering good cache with a partial set",
                        "warning",
                    )

            self.last_load_incomplete_types = (
                (["Movies"] if movies_load_incomplete else [])
                + (["TV Shows"] if tv_load_incomplete else []))
            self.last_load_complete = not self.last_load_incomplete_types

            # Freshness is only claimed for a load that actually read every
            # configured library. ScannerService suppresses a Deep Scan reload
            # when this stamp is under 5 minutes old; stamping it after a
            # partial read made it suppress the RETRY, so one unreadable library
            # kept the gap open for a whole extra scan cycle. Leaving the
            # previous (older) value alone means the stamp is not REFRESHED by a
            # partial read.
            #
            # Corrected: an earlier draft of this comment claimed the retained
            # stamp "lets the next scan reload and recover". It does the
            # opposite inside the 300s suppression window — retaining a recent
            # stamp is precisely what stops the next Deep Scan from reloading.
            # Outside 300s the stamp is stale anyway, so the claim was only true
            # where it did not matter. What actually protects the user is the
            # authority retention above: a retry would hit the same unreadable
            # library and the same empty cache, so not replacing the good list
            # is the safeguard, not the reload.
            if self.last_load_complete:
                self._last_full_load_time = time.time()

            self._emit_stats()
            restored_note = (f" (+{self.last_load_restored_rows} from cache)"
                             if self.last_load_restored_rows else "")
            self._log(
                f"Loaded Plex: {len(_movies)} movies, {tv_seasons} TV seasons{restored_note}",
                "success" if (_movies or _tv) else "warning",
            )

        except Exception as e:
            self._log(f"Error loading Plex libraries: {e}", "error")
        finally:
            self._plex_loading = False
            self._plex_loading_lock.release()

    # ── Partial-load repair ───────────────────────────────────────────

    @staticmethod
    def _cache_identity(row: Dict, is_tv: bool) -> str:
        """The plex_cache primary key for a row from EITHER source.

        Must mirror ``DatabaseManager._plex_cache_key`` exactly. The merge below
        uses it to tell "the cached copy of a row I already loaded live" from "a
        row only the cache has"; if the two sources computed different strings
        for the same file, every restored library would be duplicated in the
        index. Live movie dicts carry an explicit per-part ``key``; live TV
        season dicts do not, and their stored key is the bare rating_key.
        """
        key = row.get('key')
        if key:
            return str(key)
        if is_tv:
            return str(row.get('rating_key'))
        return f"{row.get('rating_key')}_{row.get('media_id')}"

    @staticmethod
    def _as_live_shape(row: Dict, is_tv: bool) -> Dict:
        """A cached row reshaped to look like one the live load produced.

        The two shapes are not interchangeable for MOVIES. save_plex_cache
        writes ``item.get('season', 0)``, so every cached movie row comes back
        with ``season == 0``, whereas a live movie dict has no ``season`` key at
        all. That difference is load-bearing: MatchingEngine decides whether a
        row is a TV season with ``p.get('season') == web_season``, so a cached
        movie row is a valid candidate for a season-0/specials release while its
        live twin is not. Verified against the real engine — the live-shaped row
        returns no match, the cache-shaped one matches the movie as the owned
        season.

        Restoring rows must not smuggle that difference into the index, so the
        movie rows this merge adds are put back into the live shape.
        """
        if is_tv:
            return row
        normalized = dict(row)
        normalized['season'] = None
        return normalized

    def _restore_cached_libraries(self, live_rows: List[Dict], mode: str,
                                  lib_names) -> tuple[List[Dict], int, List[str]]:
        """Merge cached rows for `lib_names` back into a live-loaded list.

        A library that could not be read is not evidence that its titles are
        gone — plex_cache still holds them, and the full_replace gate in
        load_libraries deliberately keeps them there. But the live list is what
        _build_plex_index turns into the matcher's authority, so leaving those
        titles out of it makes owned releases read Missing and hands them
        straight to auto-grab.

        Only rows the live pass did NOT produce are added, keyed on the
        plex_cache primary key. That single rule covers both failure shapes: a
        library that failed outright contributes all of its rows, while one that
        merely had per-item extraction failures contributes only the items
        missing from the fresh set — fresh data always wins over cache.

        Returns (merged_rows, restored_row_count, restored_library_names). When
        there is nothing to restore it returns `live_rows` itself, so the
        healthy path keeps assigning the exact list the load built.
        """
        if not lib_names:
            return live_rows, 0, []

        try:
            cached = self.db.load_plex_cache(mode)
        except Exception as e:
            logger.warning("Could not read the %s cache to restore %s: %s",
                           mode, sorted(lib_names), e)
            return live_rows, 0, []
        # load_plex_cache returns [] on its own errors; anything that is not a
        # list means this DatabaseManager stand-in does not implement it, and
        # there is nothing to merge.
        if not isinstance(cached, list) or not cached:
            return live_rows, 0, []

        is_tv = (mode == "TV Shows")
        live_ids = {self._cache_identity(r, is_tv) for r in live_rows}
        wanted = set(lib_names)

        # library_name is only recorded on rows written since that column
        # existed, so a cache from an older build attributes nothing and the
        # per-library filter cannot run. Restoring every not-live row instead is
        # still the right trade: the cost is that a title genuinely deleted from
        # a HEALTHY library survives one extra load (the next complete load
        # prunes it), against re-downloading titles the user already owns.
        tagged = any(r.get('library_name') for r in cached)
        if not tagged:
            self._log(
                f"{mode} cache rows carry no library name, so rows cannot be "
                f"attributed to {', '.join(sorted(wanted))} — restoring every "
                "cached row the live load did not return",
                "warning",
            )

        merged = list(live_rows)
        restored_libs = set()
        for row in cached:
            if tagged and row.get('library_name') not in wanted:
                continue
            if self._cache_identity(row, is_tv) in live_ids:
                continue
            merged.append(self._as_live_shape(row, is_tv))
            restored_libs.add(row.get('library_name') or mode)

        return merged, len(merged) - len(live_rows), sorted(restored_libs)

    # ── Data extraction ───────────────────────────────────────────────

    def _extract_movie_data(self, movie) -> Optional[List[Dict]]:
        """Extract data from a Plex movie object. Returns list of dicts (one per version)."""
        try:
            if not movie.media:
                return None

            # Reload if 4K media has missing streams
            needs_reload = any(
                m.videoResolution in ("4k", "2160") and m.parts and len(m.parts[0].videoStreams()) == 0
                for m in movie.media
            )
            if needs_reload:
                try:
                    movie.reload()
                except Exception as ex:
                    logger.warning(f"Metadata reload failed for {movie.title}: {ex}")

            results = []
            for media in movie.media:
                parts = media.parts or []
                if not parts:
                    continue
                for part_idx, part in enumerate(parts):
                    size_gb = round(part.size / (1024**3), 2) if part and part.size else 0

                    res = "?"
                    if media.videoResolution:
                        if media.videoResolution in ("4k", "2160"):
                            res = "4K"
                        elif media.videoResolution == "1080":
                            res = "1080p"
                        elif media.videoResolution == "720":
                            res = "720p"

                    dovi = False
                    hdr = False
                    for stream in part.videoStreams():
                        dovi_found = self._check_dovi(stream)
                        if dovi_found:
                            dovi = True
                            break
                        if hasattr(stream, 'colorPrimaries') and stream.colorPrimaries:
                            if 'bt2020' in stream.colorPrimaries.lower():
                                hdr = True

                    imdb_id = None
                    for guid in movie.guids:
                        if 'imdb://' in guid.id:
                            imdb_id = guid.id.replace('imdb://', '')
                            break

                    results.append({
                        'clean_title': _clean_string(movie.title),
                        'original_title': movie.title,
                        'year': movie.year or 0,
                        'res': res,
                        'size': size_gb,
                        'dovi': dovi,
                        'hdr': hdr,
                        'imdb_id': imdb_id,
                        'rating_key': movie.ratingKey,
                        # media_id is unique per version, but NOT per part — a media with
                        # multiple parts (e.g. a two-file DVD rip) reuses the same media_id
                        # for each row below. 'key' (below) is what keeps DB rows distinct.
                        'media_id': media.id,
                        'file': part.file if part else None,  # served path (may be None)
                        'language': getattr(movie, 'originalLanguage', '') or "",
                        # Per-part cache key so multi-part media don't collide in
                        # plex_cache's INSERT OR REPLACE (rating_key+media_id alone
                        # is not unique when one media has multiple parts).
                        'key': f"{movie.ratingKey}_{media.id}_{part_idx}",
                    })

            return results if results else None
        except Exception as e:
            logger.debug(f"Error extracting movie data for {movie.title}: {e}")
            return None

    def _extract_season_data(self, show, season) -> Optional[Dict]:
        """Extract data from a Plex TV season."""
        try:
            episodes = season.episodes()
            if not episodes:
                return None

            total_size = 0
            res = "?"
            dovi = False
            hdr = False

            for ep in episodes:
                if ep.media:
                    media = ep.media[0]
                    if media.parts:
                        total_size += media.parts[0].size or 0
                    # Resolution: prefer highest found across all episodes
                    if media.videoResolution:
                        if media.videoResolution in ("4k", "2160"):
                            res = "4K"
                        elif media.videoResolution == "1080" and res != "4K":
                            res = "1080p"

                    # HDR/DoVI: check all episodes so DoVI on ep2 isn't missed
                    for part in media.parts:
                        for stream in part.videoStreams():
                            if not dovi and self._check_dovi(stream):
                                dovi = True
                            if not hdr and hasattr(stream, 'colorPrimaries') and stream.colorPrimaries:
                                if 'bt2020' in stream.colorPrimaries.lower():
                                    hdr = True

            imdb_id = None
            for guid in show.guids:
                if 'imdb://' in guid.id:
                    imdb_id = guid.id.replace('imdb://', '')
                    break

            return {
                'clean_title': _clean_string(show.title),
                'original_title': show.title,
                'year': show.year or 0,
                'res': res,
                'size': round(total_size / (1024**3), 2),
                'dovi': dovi,
                'hdr': hdr,
                'imdb_id': imdb_id,
                'season': season.index,
                'episode_count': len(episodes),
                'rating_key': season.ratingKey,
                'language': getattr(show, 'originalLanguage', '') or "",
            }
        except Exception as e:
            logger.warning(f"Error extracting season data for '{show.title}' S{season.index:02d}: {e}")
            return None

    @staticmethod
    def _check_dovi(stream) -> bool:
        """Check if a video stream has Dolby Vision."""
        # Standard attribute
        if hasattr(stream, 'DOVIPresent'):
            if str(stream.DOVIPresent).lower() in ("true", "1"):
                return True
        # Raw XML data
        if hasattr(stream, '_data'):
            data = stream._data
            if isinstance(data, dict):
                for k, v in data.items():
                    if k.lower() == 'dovipresent' and str(v).lower() in ("true", "1"):
                        return True
        # Profile attributes
        if hasattr(stream, 'DOViProfile') or hasattr(stream, 'doviProfile'):
            return True
        if hasattr(stream, '_data') and isinstance(stream._data, dict):
            for k in stream._data.keys():
                if k.lower() in ('doviprofile', 'dovilevel', 'doviblpresent', 'dovielpresent'):
                    return True
        # Display title
        for attr in ('displayTitle', 'title'):
            val = getattr(stream, attr, '') or ''
            if 'dovi' in val.lower() or 'dolby vision' in val.lower():
                return True
            if re.search(r'\bDV\b', val, re.IGNORECASE):
                return True
        # Profile / codec
        profile = (getattr(stream, 'profile', '') or '').lower()
        if 'dv' in profile or 'dolby vision' in profile:
            return True
        codec = (getattr(stream, 'codec', '') or '').lower()
        if codec.startswith('dv'):
            return True
        return False

    # ── Index ─────────────────────────────────────────────────────────

    def _build_plex_index(self):
        """Build lookup index for fast Plex matching (atomic swap)."""
        new_index: Dict[str, Any] = {
            "by_imdb": {}, "by_title": {}, "by_word": {}, "all_items": []
        }

        for item in self.plex_movies + self.plex_tv:
            new_index["all_items"].append(item)
            imdb_id = item.get('imdb_id')
            if imdb_id:
                new_index["by_imdb"].setdefault(imdb_id, []).append(item)
            title = item.get('clean_title', '')
            if title:
                new_index["by_title"].setdefault(title, []).append(item)
                # Word-level index for narrowing the fuzzy candidate pool
                for word in title.split():
                    if len(word) >= 3:
                        new_index["by_word"].setdefault(word, []).append(item)

        self.plex_index = new_index

    # ── Cache validation ──────────────────────────────────────────────

    def _new_content_probe_ran(self, movie_libs: List[str], tv_libs: List[str]) -> bool:
        """Could the new-content probe have searched anything at all?

        get_recently_added() skips any library whose section won't resolve and
        returns the (possibly empty) list it collected, so an empty result only
        means "nothing new" if at least one configured library was reachable.
        Resolving ONE is enough to establish that the server answered; the loop
        stops there, so the healthy path costs a single call.

        With nothing configured there is nothing to corroborate against, so the
        caller keeps its previous behaviour rather than refreshing forever.
        """
        names = list(movie_libs) + list(tv_libs)
        if not names:
            return True
        for name in names:
            try:
                if self.plex_manager.get_library_section(name):
                    return True
            except Exception as e:
                logger.debug("Library reachability check failed for '%s': %s", name, e)
        return False

    def check_cache_status(self) -> tuple[bool, str]:
        """Check if Plex cache is valid. Returns (is_valid, message).

        Uses a lightweight timestamp-only query instead of loading all cached
        items, so this is fast even for large libraries.
        """
        try:
            movie_libs, tv_libs = self._configured_libs()

            timestamps = self.db.get_plex_cache_max_timestamp()
            if not timestamps:
                return False, "Cache not found. Full scan required."

            def _age(ts) -> float:
                return (time.time() - ts) / 3600 if ts else float('inf')

            limit_hours = self.config.get("cache_duration", 4)

            # Only check ages for content types actually present in cache
            ages = {}
            if timestamps.get("Movies"):
                ages["movies"] = _age(timestamps["Movies"])
            if timestamps.get("TV Shows"):
                ages["tv"] = _age(timestamps["TV Shows"])
            if not ages:
                return False, "Cache not found. Full scan required."

            # Checking only the ages of the content types that HAPPEN to be
            # present reports a movies-only cache as valid even when TV
            # libraries are configured — the scan then matches every TV item
            # against an index holding none (SH-H13). A configured type with
            # no rows at all is a partial cache, not a fresh one. Types that
            # aren't configured are legitimately absent and stay ignored.
            if movie_libs and not timestamps.get("Movies"):
                return False, "Movies cache missing. Full load required."
            if tv_libs and not timestamps.get("TV Shows"):
                return False, "TV Shows cache missing. Full load required."

            if any(a > limit_hours for a in ages.values()):
                age_str = ", ".join(f"{k}: {v:.1f}h" for k, v in ages.items())
                return False, f"Cache expired ({age_str})."

            # Cache is within time limit — check for new Plex content
            movie_ts = timestamps.get("Movies") or 0
            tv_ts = timestamps.get("TV Shows") or 0
            cache_ts = max(movie_ts, tv_ts)
            if (
                cache_ts > 0
                and self.plex_manager.is_connected
                and self.config.get("plex_invalidate_on_new_content", True)
            ):
                try:
                    from datetime import datetime, timezone
                    since = datetime.fromtimestamp(cache_ts, tz=timezone.utc)
                    new_items = self.plex_manager.get_recently_added(since)
                    if new_items:
                        max_age = max(ages.values()) if ages else 0
                        return False, (
                            f"Cache is {max_age:.1f}h old but "
                            f"{len(new_items)} new item(s) detected in Plex since last cache."
                        )
                    # An empty result is NOT the same answer as "nothing new".
                    # get_recently_added swallows a connect failure, a
                    # per-library exception and an outer exception alike and
                    # returns whatever it had collected, so [] is also what a
                    # mid-restart Plex produces. is_connected cannot separate
                    # them either — it is `self._server is not None`, which
                    # stays True after the server goes away. Corroborate the
                    # empty answer instead, and refresh when it can't be
                    # trusted rather than scanning against a cache that may
                    # predate real additions (SH-M25).
                    if new_items is None or not self._new_content_probe_ran(
                            movie_libs, tv_libs):
                        return False, (
                            "Could not verify whether Plex has new content "
                            "— refreshing cache."
                        )
                except Exception as e:
                    # Failing closed: the probe's whole purpose is to catch
                    # additions made since the cache was written, and a probe
                    # that raised has not ruled them out.
                    logger.warning("New content check failed: %s", e)
                    return False, f"New-content check failed ({e}) — refreshing cache."

            return True, ""
        except Exception as e:
            return False, f"Cache check failed: {e}"
