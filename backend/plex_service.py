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

            # ── Cache path ────────────────────────────────────────────
            if use_cache:
                self._log("Loading Plex data from local cache...")
                cached_movies = self.db.load_plex_cache("Movies")
                cached_tv = self.db.load_plex_cache("TV Shows")

                if cached_movies or cached_tv:
                    self.plex_movies = cached_movies
                    self.plex_tv = cached_tv
                    self.stats['plex_4k'] = len({m.get('imdb_id') or m.get('clean_title', '') for m in self.plex_movies if m.get('res') == '4K'} - {''})
                    self.stats['plex_1080'] = len({m.get('imdb_id') or m.get('clean_title', '') for m in self.plex_movies if m.get('res') == '1080p'} - {''})
                    self.stats['tv_seasons'] = len(self.plex_tv)
                    self._build_plex_index()
                    self._emit_stats()
                    self._last_full_load_time = time.time()
                    self._log(
                        f"Loaded Cache: {len(self.plex_movies)} movies, {self.stats['tv_seasons']} seasons",
                        "success",
                    )
                    return
                else:
                    self._log("Cache empty, falling back to full load...", "warning")

            # ── Full load ─────────────────────────────────────────────
            # Use `or` so an explicit empty list falls through to the next fallback.
            # Priority: movie_libs (user-assigned) → known_movie_libraries (legacy key)
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

            # ── Movies ────────────────────────────────────────────────
            for lib_name in movie_libs:
                if progress_callback:
                    progress_callback(current_lib_idx / total_libs, f"Loading {lib_name}...")

                try:
                    lib = self.plex_manager.get_library_section(lib_name)
                    if not lib:
                        self._log(f"Movie library '{lib_name}' not found", "warning")
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
                except Exception as e:
                    self._log(f"Error loading movie library '{lib_name}': {e}", "error")
                    movies_load_incomplete = True

                current_lib_idx += 1

            # ── TV Shows ──────────────────────────────────────────────
            for lib_name in tv_libs:
                if progress_callback:
                    progress_callback(current_lib_idx / total_libs, f"Loading {lib_name}...")

                try:
                    lib = self.plex_manager.get_library_section(lib_name)
                    if not lib:
                        self._log(f"TV library '{lib_name}' not found", "warning")
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

                    # Diagnostics
                    diag = []
                    if tv_errors:
                        diag.append(f"{tv_errors} errored")
                    if tv_no_seasons:
                        diag.append(f"{tv_no_seasons} had no seasons")
                    if tv_all_specials:
                        diag.append(f"{tv_all_specials} only specials")
                    if tv_extract_fail:
                        diag.append(f"{tv_extract_fail} season extracts failed")

                    if diag:
                        self._log(
                            f"TV loading: {total_items} shows, {tv_seasons} seasons. "
                            f"Issues: {', '.join(diag)}."
                            + (f" First error: {first_error}" if first_error else ""),
                            "warning" if tv_seasons > 0 else "error",
                        )
                    else:
                        self._log(f"Loaded {tv_seasons} seasons from {total_items} shows in '{lib_name}'", "success")

                except Exception as e:
                    self._log(f"Error loading TV library '{lib_name}': {e}", "error")
                    tv_load_incomplete = True

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

            # Atomic swap — UI reads see complete state, never partial
            self.plex_movies = _movies
            self.plex_tv = _tv

            self._build_plex_index()

            if not self.plex_movies and not self.plex_tv:
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
                if self.plex_movies and not movies_load_incomplete:
                    self.db.save_plex_cache(self.plex_movies, "Movies", full_replace=True)
                elif not self.plex_movies:
                    self._log("Skipping Movies cache save — load returned 0 (preserving existing cache)", "warning")
                else:
                    self._log(
                        "Movies load was interrupted (library error) — skipping full-replace "
                        "cache save to avoid clobbering good cache with a partial set",
                        "warning",
                    )
                if self.plex_tv and not tv_load_incomplete:
                    self.db.save_plex_cache(self.plex_tv, "TV Shows", full_replace=True)
                elif not self.plex_tv:
                    self._log("Skipping TV Shows cache save — load returned 0 (preserving existing cache)", "warning")
                else:
                    self._log(
                        "TV load was interrupted (library error) — skipping full-replace "
                        "cache save to avoid clobbering good cache with a partial set",
                        "warning",
                    )

            self._last_full_load_time = time.time()
            self._emit_stats()
            self._log(
                f"Loaded Plex: {len(self.plex_movies)} movies, {tv_seasons} TV seasons",
                "success" if (self.plex_movies or self.plex_tv) else "warning",
            )

        except Exception as e:
            self._log(f"Error loading Plex libraries: {e}", "error")
        finally:
            self._plex_loading = False
            self._plex_loading_lock.release()

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

    def check_cache_status(self) -> tuple[bool, str]:
        """Check if Plex cache is valid. Returns (is_valid, message).

        Uses a lightweight timestamp-only query instead of loading all cached
        items, so this is fast even for large libraries.
        """
        try:
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
                except Exception as e:
                    logger.debug("New content check failed: %s", e)

            return True, ""
        except Exception as e:
            return False, f"Cache check failed: {e}"
