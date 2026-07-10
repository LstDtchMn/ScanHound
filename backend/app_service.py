"""AppService — Core orchestrator for ScanHound.

Manages configuration, logging, caching, and application lifecycle.
Framework-agnostic: no UI dependencies.
"""

import functools
import json
import logging
import os
import re
import shutil
import time
import threading
import requests
from collections import OrderedDict, deque
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from dotenv import load_dotenv

from backend.config import (
    AppConfig, SETTINGS_PRESETS, DEFAULT_CONFIG, CONFIG_FILE, _LEGACY_CONFIG_FILE,
    CACHE_FILE, HISTORY_FILE, LOG_FILE, _BASE_DIR,
    API_RATE_LIMIT_DELAY, MAX_RETRIES, RETRY_BACKOFF_FACTOR,
    get_default_config, validate_config,
)
from backend.database import DatabaseManager
from backend.plex_manager import PlexManager, migrate_library_config

logger = logging.getLogger(__name__)

# ── Application metadata ──────────────────────────────────────────────
APP_NAME = "ScanHound"
APP_VERSION = "3.0"

# Bind-mounted data dir the host detector reads (design §5/§9). Fixed path so the
# host script never needs config.py's %APPDATA% resolution.
_DV_DATA_DIR = os.environ.get("SCANHOUND_DATA_DIR", "/data")
DV_HOST_JSON = os.path.join(_DV_DATA_DIR, "dv_host.json")

_DV_EXPORT_DEFAULTS = {
    "dv_library_roots": "",
    "dv_detection": False,
    "dv_file_tagging": False,
    "dv_label_vocab": '{"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}',
}


def export_dv_host_config(config, dest):
    """Write the DV subset of *config* to *dest* (JSON) for the host detector.

    Only the four DV keys are exported — never secrets. Missing keys fall back to
    defaults. Returns the dict written. Atomic replace; parent dir auto-created.
    """
    payload = {k: config.get(k, default) for k, default in _DV_EXPORT_DEFAULTS.items()}
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = f"{dest}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    return payload

# ── TMDB constants ────────────────────────────────────────────────────
TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
TMDB_POSTER_SIZE = "w185"
TMDB_POSTER_LARGE = "w342"

TMDB_GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi", 10766: "Soap", 10767: "Talk", 10768: "War",
}

TMDB_LANGUAGE_MAP = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "cn": "Chinese", "hi": "Hindi",
    "ar": "Arabic", "nl": "Dutch", "pl": "Polish", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "tr": "Turkish",
    "th": "Thai", "id": "Indonesian", "vi": "Vietnamese", "cs": "Czech",
    "hu": "Hungarian", "ro": "Romanian", "el": "Greek", "he": "Hebrew",
    "uk": "Ukrainian", "fa": "Persian", "bn": "Bengali", "ms": "Malay",
    "tl": "Filipino", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam",
    "kn": "Kannada", "mr": "Marathi", "pa": "Punjabi", "sr": "Serbian",
    "hr": "Croatian", "bg": "Bulgarian", "sk": "Slovak", "sl": "Slovenian",
    "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian", "is": "Icelandic",
    "ga": "Irish", "ka": "Georgian", "hy": "Armenian", "sq": "Albanian",
    "mk": "Macedonian", "bs": "Bosnian", "mt": "Maltese", "cy": "Welsh",
    "af": "Afrikaans", "sw": "Swahili", "zu": "Zulu", "am": "Amharic",
    "ne": "Nepali", "si": "Sinhala", "km": "Khmer", "lo": "Lao",
    "my": "Burmese", "mn": "Mongolian", "ur": "Urdu", "ps": "Pashto",
    "ku": "Kurdish", "uz": "Uzbek", "kk": "Kazakh", "az": "Azerbaijani",
    "gl": "Galician", "ca": "Catalan", "eu": "Basque", "la": "Latin",
    "eo": "Esperanto", "jv": "Javanese", "su": "Sundanese",
}

# ── Status/color constants ────────────────────────────────────────────
STATUS_MISSING = "Missing"
STATUS_DOWNLOADED = "Downloaded"
STATUS_IN_LIBRARY = "In Library"
STATUS_IN_LIBRARY_CHECK = "\u2713 In Library"
STATUS_UPGRADE_4K = "UPGRADE (4K)"
STATUS_UPGRADE_SIZE = "UPGRADE (Size)"
STATUS_UPGRADE_SIZE_DV = "UPGRADE (+DV)"
STATUS_DV_UPGRADE = "UPGRADE (DV)"

COLOR_MISSING = "#e74c3c"
COLOR_DOWNLOADED = "#17a2b8"
COLOR_IN_LIBRARY = "#27ae60"
COLOR_UPGRADE = "#f39c12"
COLOR_DV_UPGRADE = "#9b59b6"

RESOLUTION_ORDER = {"?": 0, "SD": 1, "720p": 2, "1080p": 3, "4K": 4}

# ── Network constants ─────────────────────────────────────────────────
MAX_RETRIES_NET = MAX_RETRIES
RETRY_BACKOFF = RETRY_BACKOFF_FACTOR


# ── LRU Cache ─────────────────────────────────────────────────────────
class LRUCache:
    """Thread-safe LRU cache with bounded size."""

    def __init__(self, maxsize: int = 1000):
        self.maxsize = maxsize
        self.cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self.cache

    def __getitem__(self, key):
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            raise KeyError(key)

    def __setitem__(self, key, value):
        with self._lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            while len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def clear(self):
        with self._lock:
            self.cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self.cache)


# ── Network retry decorator ──────────────────────────────────────────
def retry_request(func):
    """Decorator to retry network requests with exponential backoff."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES_NET):
            try:
                return func(*args, **kwargs)
            except (requests.RequestException, requests.Timeout, ConnectionError) as e:
                if attempt == MAX_RETRIES_NET - 1:
                    logger.error(f"Request failed after {MAX_RETRIES_NET} attempts: {e}")
                    raise
                wait_time = API_RATE_LIMIT_DELAY * (RETRY_BACKOFF ** attempt)
                logger.warning(
                    f"Request failed (attempt {attempt + 1}/{MAX_RETRIES_NET}), "
                    f"retrying in {wait_time:.2f}s: {e}"
                )
                time.sleep(wait_time)
    return wrapper


# ── Credential masking for log output ─────────────────────────────────
# Patterns that look like tokens, passwords, or API keys in log messages
_CREDENTIAL_PATTERNS = re.compile(
    r'((?:token|password|api_key|secret|plex_token|X-Plex-Token)'
    r'\s*[=:]\s*)([^\s,\'"}\]]{4,})',
    re.IGNORECASE,
)


class CredentialMaskingFilter(logging.Filter):
    """Log filter that masks credentials in log messages and format args."""

    @staticmethod
    def _mask(s: str) -> str:
        return _CREDENTIAL_PATTERNS.sub(lambda m: m.group(1) + m.group(2)[:3] + '***', s)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._mask(record.msg)
        # Also mask %-format arguments so "token: %s" % token is protected
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._mask(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: self._mask(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


# ── Logging ───────────────────────────────────────────────────────────

class InMemoryLogBuffer(logging.Handler):
    """Circular in-memory buffer that captures all log entries from startup.

    This lets the UI log viewer pre-populate with messages emitted before
    the QML component was loaded — even deep-scan output, Plex connection
    events, etc.  The buffer holds up to ``maxsize`` entries; oldest are
    dropped automatically.
    """

    # Map Python level names → the level strings the QML log viewer expects
    _LEVEL_MAP = {
        "DEBUG": "debug",
        "INFO": "info",
        "WARNING": "warning",
        "ERROR": "error",
        "CRITICAL": "error",
    }

    def __init__(self, maxsize: int = 2000):
        super().__init__()
        self._buffer: deque = deque(maxlen=maxsize)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = self._LEVEL_MAP.get(record.levelname, "info")
            with self._lock:
                self._buffer.append({"message": msg, "level": level})
        except Exception:
            pass

    def get_entries(self) -> list:
        """Return a snapshot of the buffer as a plain list (thread-safe)."""
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


def setup_logging(
    debug_mode: bool = False,
    clear_on_start: bool = False,
    buffer: "InMemoryLogBuffer | None" = None,
) -> logging.Logger:
    """Set up application logging with rotating file handler.

    Args:
        debug_mode: When True, sets log level to DEBUG (captures everything).
        clear_on_start: When True, deletes the existing log file before setup.
        buffer: Optional in-memory buffer handler to attach to the root logger.
    """
    log_level = logging.DEBUG if debug_mode else logging.INFO

    if clear_on_start and os.path.exists(LOG_FILE):
        try:
            os.remove(LOG_FILE)
        except Exception as e:
            import sys
            sys.stderr.write(f"Could not clear log file: {e}\n")

    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%H:%M:%S'
    )
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    # Add credential masking filter to all handlers
    mask_filter = CredentialMaskingFilter()
    file_handler.addFilter(mask_filter)
    console_handler.addFilter(mask_filter)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Attach in-memory buffer if provided
    if buffer is not None:
        buffer.setFormatter(formatter)
        buffer.setLevel(log_level)
        buffer.addFilter(mask_filter)
        root.addHandler(buffer)

    # Suppress noisy third-party loggers
    for name in ("urllib3", "requests", "plexapi", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return root


# ── Title normalization ───────────────────────────────────────────────
def clean_string(s: str) -> str:
    """Normalize a title string for comparison."""
    if not s:
        return ""
    normalized = s.lower().strip()
    # Remove parenthesised years (e.g. "The Thing (1982)") — safe even for
    # year-as-title because the word form ("1917", "2001") is kept below.
    normalized = re.sub(r'\((\d{4})\)', '', normalized)
    # Remove standalone years ONLY when they are not the sole word in the
    # string (prevents "1917", "2001", "1984" from normalising to "").
    year_stripped = re.sub(r'\b(19|20)\d{2}\b', '', normalized).strip()
    if year_stripped:
        normalized = year_stripped
    normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def normalize_title(title: str) -> str:
    """Normalize a title for dedup/lookup (alias for clean_string)."""
    return clean_string(title)


# ── Idle detection (Windows) ──────────────────────────────────────────
def _get_idle_seconds() -> int:
    """Return system idle time in seconds (Windows only, 0 elsewhere)."""
    import platform
    if platform.system() != "Windows":
        return 0
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return millis // 1000
    except Exception:
        pass
    return 0


# ── AppService ────────────────────────────────────────────────────────
class AppService:
    """Central backend service — owns config, DB, caches, and lifecycle."""

    def __init__(self):
        self.config: Dict[str, Any] = {}
        self.db: Optional[DatabaseManager] = None
        self.plex_manager: Optional[PlexManager] = None
        self.tmdb_cache = LRUCache(maxsize=2000)
        self.omdb_cache = LRUCache(maxsize=2000)
        self._log_callback: Optional[Callable[[str, str], None]] = None
        self._shutdown_hooks: List[Callable] = []

        # In-memory log buffer — created before startup() so it captures
        # all log entries from startup onward (before the UI log tab loads)
        self.log_buffer = InMemoryLogBuffer(maxsize=2000)

        # Optional subsystems (initialized in startup if available)
        self.notification_manager = None
        self.watchlist_manager = None
        self.stats_dashboard = None
        self._scheduler_thread: Optional[threading.Thread] = None
        self._scheduler_stop = threading.Event()
        self._scan_trigger: Optional[Callable] = None
        self._config_lock = threading.RLock()

        # Maintenance loop — trash retention sweep + periodic WAL checkpoint.
        # Always started (unlike the scan scheduler, which is opt-in): both
        # tasks are self-contained housekeeping with no user-visible side
        # effects, so they don't need a settings gate.
        self._maintenance_thread: Optional[threading.Thread] = None
        self._maintenance_stop = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def startup(self) -> List[str]:
        """Initialize all backend components. Returns list of warnings."""
        warnings: List[str] = []

        # Config (use defaults on failure)
        try:
            self.config = self.load_config()
        except Exception as e:
            self.config = get_default_config()
            warnings.append(f"Config load failed, using defaults: {e}")

        # Logging
        try:
            self.logger = setup_logging(
                debug_mode=self.config.get("debug_mode", False),
                clear_on_start=self.config.get("clear_logs_startup", False),
                buffer=self.log_buffer,
            )
        except Exception as e:
            self.logger = logging.getLogger(__name__)
            warnings.append(f"Logging setup failed: {e}")

        # Migrate legacy library keys → movie_libs / tv_libs if still empty
        # Save immediately so a Settings > Cancel can't undo the migration.
        _lib_migrated = False
        if not self.config.get("movie_libs"):
            known_movie = self.config.get("known_movie_libraries", [])
            if known_movie:
                self.config["movie_libs"] = list(known_movie)
                logger.info("Migrated known_movie_libraries → movie_libs: %s", known_movie)
                _lib_migrated = True
        if not self.config.get("tv_libs"):
            known_tv = self.config.get("known_tv_libraries", [])
            if known_tv:
                self.config["tv_libs"] = list(known_tv)
                logger.info("Migrated known_tv_libraries → tv_libs: %s", known_tv)
                _lib_migrated = True
        if _lib_migrated:
            try:
                self.save_config()
                logger.info("Migrated library config saved to disk")
            except Exception as e:
                logger.warning("Could not save migrated library config: %s", e)

        # Database
        try:
            self.db = DatabaseManager()
        except Exception as e:
            warnings.append(f"Database init failed: {e}")
        if self.db is not None:
            try:
                self._migrate_legacy_persistence()
                logger.info(
                    "Recovered %d download history entries from database.",
                    self.db.get_history_count(),
                )
            except Exception as e:
                warnings.append(f"Legacy persistence migration failed: {e}")

        # PlexManager
        try:
            self.plex_manager = PlexManager()
        except Exception as e:
            self.plex_manager = None
            warnings.append(f"PlexManager init failed: {e}")

        # Optional subsystems — best-effort, don't block startup
        self._init_optional_subsystems()

        for w in warnings:
            logger.warning(w)

        return warnings

    def _legacy_persistence_candidates(self) -> List[Tuple[str, str]]:
        """Return legacy JSON history/cache path pairs in migration priority order."""
        candidates: List[Tuple[str, str]] = []

        def add_dir(dir_path: str):
            if not dir_path:
                return
            history_names = ("download_history.json", "history.json")
            cache_path = os.path.join(dir_path, "cache.json")
            for history_name in history_names:
                candidates.append((os.path.join(dir_path, history_name), cache_path))

        add_dir(_BASE_DIR)

        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
            legacy_data_dir = os.path.join(base, "MediaScout")
        else:
            legacy_data_dir = os.path.join(
                os.path.expanduser("~"), ".local", "share", "mediascout"
            )
        add_dir(legacy_data_dir)

        seen: set[Tuple[str, str]] = set()
        ordered: List[Tuple[str, str]] = []
        for pair in candidates:
            if pair in seen:
                continue
            seen.add(pair)
            ordered.append(pair)
        return ordered

    def _migrate_legacy_persistence(self) -> Tuple[int, int]:
        """Import legacy JSON history/cache files into the current database."""
        if not self.db:
            return 0, 0

        migrated_history = 0
        migrated_cache = 0
        for history_file, cache_file in self._legacy_persistence_candidates():
            hist_count, cache_count = self.db.migrate_json_data(history_file, cache_file)
            migrated_history += hist_count
            migrated_cache += cache_count

        if migrated_history or migrated_cache:
            logger.info(
                "Legacy migration imported %d history items and %d cache items.",
                migrated_history,
                migrated_cache,
            )
        return migrated_history, migrated_cache

    def _init_optional_subsystems(self):
        """Initialize optional backend systems (notifications, watchlist, analytics)."""
        # Notifications
        try:
            from backend.notifications import NotificationManager
            self.notification_manager = NotificationManager()
            logger.info("NotificationManager initialized")
        except Exception as e:
            logger.debug(f"NotificationManager not available: {e}")

        # Watchlist — routed through the shared DatabaseManager (self.db) so
        # its reads/writes serialize with every other DB access instead of
        # opening a second sqlite3 connection to the same file (see B1).
        try:
            from backend.watchlist import WatchlistManager
            self.watchlist_manager = WatchlistManager(db_manager=self.db)
            logger.info("WatchlistManager initialized")
        except Exception as e:
            logger.debug(f"WatchlistManager not available: {e}")

        # Analytics — same shared-DatabaseManager routing as watchlist above.
        try:
            from backend.analytics import StatsDashboard
            self.stats_dashboard = StatsDashboard(db_manager=self.db)
            logger.info("StatsDashboard initialized")
        except Exception as e:
            logger.debug(f"StatsDashboard not available: {e}")

        # Scheduler
        if self.config.get("scheduler_enabled", False):
            self._start_scheduler()

        # Maintenance loop (trash sweep + WAL checkpoint) — always on.
        self._run_maintenance_pass()  # once immediately at startup
        self._start_maintenance_loop()

    def _run_maintenance_pass(self):
        """Run one maintenance pass: trash retention sweep + DB WAL checkpoint.

        Fail-safe by design — either task's exception is logged and
        swallowed so a single bad pass never crashes the loop (or startup).
        Reads ``trash_retention_days`` from the live config each call so a
        settings change takes effect on the next scheduled run.
        """
        try:
            from backend.rename import fileops
            retention_days = self.config.get("trash_retention_days", 30)
            summary = fileops.sweep_trash(retention_days, roots=fileops.all_trash_roots())
            if summary.get("files_deleted"):
                logger.info(
                    "Trash sweep: removed %d file(s), freed %d bytes (retention=%dd)",
                    summary["files_deleted"], summary["bytes_freed"], retention_days)
        except Exception:
            logger.exception("Trash retention sweep failed (non-fatal)")

        try:
            if self.db is not None:
                self.db.checkpoint()
        except Exception:
            logger.exception("Periodic WAL checkpoint failed (non-fatal)")

        try:
            if self.db is not None and self.config.get("pipeline_reconcile_enabled", True):
                from backend.pipeline_service import reconcile_batch
                jd_method = self.config.get("jd_method", "folder")
                n = reconcile_batch(self.db, jd_method=jd_method)
                if n:
                    logger.info("Pipeline reconcile: checked %d grab(s)", n)
        except Exception:
            logger.exception("Pipeline reconcile failed (non-fatal)")

    def _start_maintenance_loop(self, interval_seconds: float = 3600.0):
        """Start the hourly trash-sweep + WAL-checkpoint background thread."""
        if self._maintenance_thread and self._maintenance_thread.is_alive():
            return
        self._maintenance_stop.clear()

        def _loop():
            while not self._maintenance_stop.wait(interval_seconds):
                self._run_maintenance_pass()

        self._maintenance_thread = threading.Thread(
            target=_loop, name="maintenance", daemon=True)
        self._maintenance_thread.start()
        logger.info("Maintenance loop started (every %.0fs)", interval_seconds)

    def set_scan_trigger(self, callback: Optional[Callable]):
        """Register a callback the scheduler invokes to start a scan."""
        self._scan_trigger = callback

    def _start_scheduler(self):
        """Start the background scan scheduler thread.

        Checks every 60 seconds whether the next scan interval has elapsed.
        Supports optional idle-only mode via Windows GetLastInputInfo.
        """
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._scheduler_stop.clear()
        idle_threshold = 300  # 5 minutes

        def _scheduler_loop():
            while not self._scheduler_stop.wait(60):
                if self._scheduler_stop.is_set():
                    break
                # Re-read config each iteration so changes take effect immediately
                interval_hours = max(1, self.config.get("scheduler_interval", 24))
                interval_seconds = interval_hours * 3600
                only_when_idle = self.config.get("scheduler_only_when_idle", False)

                last = self.config.get("last_scan_time", 0)
                now = time.time()
                if now - last < interval_seconds:
                    continue

                # Idle check (Windows only)
                if only_when_idle:
                    idle_secs = _get_idle_seconds()
                    if idle_secs < idle_threshold:
                        continue

                with self._config_lock:
                    self.config["last_scan_time"] = now
                    self.save_config()
                logger.info("Scheduled scan triggered")
                if self._scan_trigger:
                    try:
                        self._scan_trigger()
                    except Exception as e:
                        logger.error(f"Scheduled scan trigger failed: {e}")
                elif self._log_callback:
                    try:
                        self._log_callback(
                            "Scheduled scan interval reached (no trigger registered)",
                            "warning"
                        )
                    except Exception:
                        pass

        self._scheduler_thread = threading.Thread(
            target=_scheduler_loop, name="scheduler", daemon=True
        )
        self._scheduler_thread.start()
        initial_interval = max(1, self.config.get("scheduler_interval", 24))
        logger.info(f"Scheduler started (every {initial_interval}h)")

    def shutdown(self):
        """Gracefully close all resources."""
        # Stop scheduler and wait for it to exit
        self._scheduler_stop.set()
        sched = getattr(self, '_scheduler_thread', None)
        if sched and sched.is_alive():
            sched.join(timeout=3)

        # Stop the maintenance loop (trash sweep + WAL checkpoint) too.
        maint_stop = getattr(self, '_maintenance_stop', None)
        if maint_stop is not None:
            maint_stop.set()
        maint = getattr(self, '_maintenance_thread', None)
        if maint and maint.is_alive():
            maint.join(timeout=3)

        # Run controller shutdown hooks (stop workers before DB close)
        for fn in list(self._shutdown_hooks):
            try:
                fn()
            except Exception as e:
                logger.warning(f"Shutdown hook error: {e}")

        # Close optional subsystems
        if self.watchlist_manager:
            try:
                self.watchlist_manager.close()
            except Exception as e:
                logger.warning("Error closing watchlist manager: %s", e)

        # Close database last
        if self.db:
            self.db.close()

        logger.info("AppService shutdown complete")

    def add_shutdown_hook(self, fn: Callable):
        """Register a callable to run during shutdown (before DB close)."""
        self._shutdown_hooks.append(fn)

    # ── Logging bridge ────────────────────────────────────────────────

    def set_log_callback(self, fn: Callable[[str, str], None]):
        """Register a callback for log messages: fn(message, level)."""
        self._log_callback = fn

    def log(self, message: str, level: str = "info"):
        """Log a message and forward to the UI callback if registered."""
        getattr(logger, level if level != "success" else "info", logger.info)(message)
        if self._log_callback:
            try:
                self._log_callback(message, level)
            except Exception:
                pass

    # ── Config management ─────────────────────────────────────────────

    def load_config(self) -> Dict[str, Any]:
        """Load config from file + environment overrides."""
        load_dotenv()
        config = get_default_config()

        # One-time migration: move config from project root (inside sync path)
        # to the platform AppData directory so credentials aren't cloud-synced.
        if not os.path.exists(CONFIG_FILE) and os.path.exists(_LEGACY_CONFIG_FILE):
            try:
                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                shutil.move(_LEGACY_CONFIG_FILE, CONFIG_FILE)
                logger.info("Migrated config to %s (credentials no longer in sync folder)", CONFIG_FILE)
            except OSError as e:
                logger.warning("Config migration failed, reading from legacy path: %s", e)

        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
                config.update(file_config)
                # Migrate legacy plex_mode → plex_connection_mode
                if "plex_mode" in config and "plex_connection_mode" not in file_config:
                    config["plex_connection_mode"] = config.pop("plex_mode")
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading config: {e}, using defaults")

        env_overrides = {
            "plex_url": os.getenv("PLEX_URL"),
            "plex_token": os.getenv("PLEX_TOKEN"),
            "plex_password": os.getenv("PLEX_PASSWORD"),
            "tmdb_api_key": os.getenv("TMDB_API_KEY"),
            "omdb_api_key": os.getenv("OMDB_API_KEY"),
            "jd_email": os.getenv("JD_EMAIL"),
            "jd_password": os.getenv("JD_PASSWORD"),
            "jd_device": os.getenv("JD_DEVICE"),
            "cuty_email": os.getenv("CUTY_EMAIL"),
            "cuty_password": os.getenv("CUTY_PASSWORD"),
            "adithd_username": os.getenv("ADITHD_USERNAME"),
            "adithd_password": os.getenv("ADITHD_PASSWORD"),
            "smtp_username": os.getenv("SMTP_USERNAME"),
            "smtp_password": os.getenv("SMTP_PASSWORD"),
            "discord_webhook": os.getenv("DISCORD_WEBHOOK"),
            "slack_webhook": os.getenv("SLACK_WEBHOOK"),
            "pushover_user": os.getenv("PUSHOVER_USER"),
            "pushover_token": os.getenv("PUSHOVER_TOKEN"),
            "webhook_url": os.getenv("WEBHOOK_URL"),
        }
        for key, value in env_overrides.items():
            if value is not None:
                config[key] = value

        config = migrate_library_config(config)
        config = validate_config(config)
        return config

    # Keys that must never be silently blanked by a save
    _SENSITIVE_KEYS = {
        "plex_token", "tmdb_api_key", "omdb_api_key", "cuty_password",
        "adithd_password", "discord_webhook", "smtp_password",
        "pushover_token", "plex_url", "slack_webhook", "webhook_url",
        "pushover_user", "jd_email", "jd_password",
    }

    def save_config(self):
        """Save config to JSON file atomically with restricted permissions.

        Protects sensitive keys: if the in-memory config has an empty value
        for a sensitive key but the disk file has a non-empty value, the
        disk value is preserved (prevents accidental credential wipe).
        """
        with self._config_lock:
            try:
                # Preserve sensitive values from disk if memory has empty
                # BUT respect explicit clears (tracked in _cleared_keys)
                if os.path.exists(CONFIG_FILE):
                    try:
                        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                            disk_config = json.load(f)
                        cleared = getattr(self, '_cleared_keys', set())
                        for key in self._SENSITIVE_KEYS:
                            if key in cleared:
                                continue  # User explicitly cleared this key
                            disk_val = disk_config.get(key, "")
                            mem_val = self.config.get(key, "")
                            if disk_val and not mem_val:
                                self.config[key] = disk_val
                    except (json.JSONDecodeError, IOError):
                        pass  # Can't read disk, proceed with memory values

                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                temp_file = f"{CONFIG_FILE}.tmp"
                # Create temp file with owner-only permissions (0o600)
                fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_file, CONFIG_FILE)
                # Ensure final file also has restricted permissions
                try:
                    os.chmod(CONFIG_FILE, 0o600)
                except OSError:
                    pass  # Best effort on platforms that don't support chmod
                try:
                    export_dv_host_config(self.config, DV_HOST_JSON)
                except Exception as e:
                    logger.warning("dv_host.json export failed: %s", e)
            except (IOError, OSError) as e:
                logger.error(f"Failed to save config: {e}")

    def validate_config_values(self) -> Dict[str, List[str]]:
        """Validate config and return warnings/errors."""
        warnings: List[str] = []
        errors: List[str] = []

        numeric_validations = {
            'min_size_mb': (0, 100000, 200),
            'cache_duration': (0, 168, 4),
            'upgrade_sensitivity': (0, 100, 10),
            'scheduler_interval': (1, 168, 24),
            'scan_threads': (1, 50, 10),
            'tv_match_threshold': (0, 100, 90),
            'low_match_threshold': (0, 100, 80),
            'movie_match_threshold': (0, 100, 85),
            'year_tolerance': (0, 10, 1),
            'trash_retention_days': (1, 365, 30),
        }
        for key, (min_val, max_val, default_val) in numeric_validations.items():
            if key in self.config:
                val = self.config[key]
                if not isinstance(val, (int, float)):
                    warnings.append(f"Config '{key}' must be numeric, using default: {default_val}")
                    self.config[key] = default_val
                elif val < min_val or val > max_val:
                    warnings.append(f"Config '{key}' out of range [{min_val}-{max_val}], corrected to {default_val}")
                    self.config[key] = default_val

        if 'plex_url' in self.config:
            url = self.config['plex_url']
            if url and not url.startswith(('http://', 'https://')):
                warnings.append("Plex URL should start with http:// or https://")

        boolean_fields = [
            'use_tmdb', 'show_rating', 'show_votes', 'show_rt', 'show_rg',
            'show_nf', 'show_links', 'jd_enabled', 'rule_1080_4k',
            'rule_1080_4k_size', 'rule_1080_1080', 'rule_4k_4k', 'rule_dv',
            'strict_resolution', 'debug_mode', 'source_2160p', 'source_remux',
            'source_tv_packs', 'scheduler_enabled', 'clear_logs_startup',
            'plex_invalidate_on_new_content',
        ]
        for key in boolean_fields:
            if key in self.config and not isinstance(self.config[key], bool):
                warnings.append(f"Config '{key}' must be boolean. Converting.")
                self.config[key] = bool(self.config[key])

        if self.config.get('plex_refresh_mode') not in ("auto", "force_refresh", "cache_only"):
            warnings.append("Config 'plex_refresh_mode' is invalid, using default: auto")
            self.config['plex_refresh_mode'] = "auto"

        return {'warnings': warnings, 'errors': errors}

    def apply_preset(self, preset_name: str) -> bool:
        """Apply a settings preset."""
        if preset_name not in SETTINGS_PRESETS:
            return False
        preset = SETTINGS_PRESETS[preset_name]
        for key, value in preset.items():
            if key != "description":
                self.config[key] = value
        self.save_config()
        return True

    # ── Download history ──────────────────────────────────────────────

    def load_download_history(self) -> Set[str]:
        """Load download history URLs from database."""
        try:
            with self.db.transaction() as conn:
                if not conn:
                    return set()
                rows = conn.execute("SELECT url FROM downloads").fetchall()
                return {row[0] for row in rows}
        except Exception as e:
            logger.error(f"Failed to load history: {e}")
            return set()
