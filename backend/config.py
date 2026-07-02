"""Configuration module - Centralized configuration types and constants.

This module provides:
- TypedDict definitions for type-safe configuration
- Default configuration values
- Settings presets
- Shared constants

Separating configuration into its own module resolves circular imports
between movie_app.py and settings_dialog.py.
"""

import copy
import os
from typing import TypedDict, List, Literal


class AppConfig(TypedDict, total=False):
    """Type definition for application configuration.

    All fields are optional (total=False) to support partial configs
    and preset overrides.
    """
    # Plex Connection
    plex_url: str
    plex_token: str
    plex_server_id: str
    plex_connection_mode: Literal["direct", "account"]  # direct=LAN, account=plex.tv
    plex_username: str
    plex_password: str
    plex_server_name: str  # server name for account mode

    # API Keys
    tmdb_api_key: str
    omdb_api_key: str
    use_tmdb: bool

    # Size & Resolution
    min_size_mb: int
    pref_res: Literal["Prefer 4K", "Prefer 1080p", "Any"]

    # Display Options
    show_rating: bool
    show_votes: bool
    show_rt: bool
    show_rg: bool
    show_nf: bool
    show_links: bool
    show_genres: bool

    # Cache Settings
    cache_duration: int  # hours
    plex_refresh_mode: Literal["auto", "force_refresh", "cache_only"]
    plex_invalidate_on_new_content: bool

    # Filtering
    ignore_keywords: str

    # Upgrade Rules
    upgrade_sensitivity: int  # percentage
    rule_1080_4k: bool
    rule_1080_4k_size: bool
    rule_1080_1080: bool
    rule_4k_4k: bool
    rule_dv: bool
    strict_resolution: bool

    # Libraries
    movie_libs: List[str]
    tv_libs: List[str]
    known_libraries: List[str]

    # JDownloader Integration
    jd_enabled: bool
    jd_method: Literal["folder", "api"]
    jd_folder: str
    jd_movies_folder: str
    jd_tv_folder: str
    jd_email: str
    jd_password: str
    jd_device: str

    # Filtering
    exclude_720p: bool

    # Sources
    source_2160p: bool
    source_remux: bool
    source_tv_packs: bool

    # DDLBase / Cuty.io
    ddlbase_enabled: bool
    cuty_email: str
    cuty_password: str

    # Adit-HD Forum
    adithd_enabled: bool
    adithd_username: str
    adithd_password: str
    adithd_auto_reply: bool
    adithd_preferred_host: Literal["rapidgator", "nitroflare", "1fichier"]

    # Scheduler
    scheduler_enabled: bool
    scheduler_interval: int  # hours
    last_scan_time: float  # timestamp

    # Background pre-cache scanning (pre-fetch results so the app opens fast)
    background_scan_enabled: bool
    background_scan_interval_hours: int
    background_scan_pages: int
    background_scan_sources: List[str]
    background_scan_retain_days: int
    background_scan_last_run: float  # timestamp of the last completed run

    # Auto-rename (post-extraction) + Plex sort + optional Ollama assist
    auto_rename_enabled: bool
    auto_rename_confidence_threshold: int
    auto_rename_require_confirmation: bool
    auto_rename_move_method: str
    auto_rename_movie_library: str
    auto_rename_movie_library_4k: str
    auto_rename_tv_library: str
    auto_rename_template_movie: str
    auto_rename_template_tv: str
    auto_rename_plex_sort_titles: bool
    auto_rename_llm_enabled: bool
    ollama_base_url: str
    ollama_model: str

    # Dolby Vision host-detector + labeler
    dv_library_roots: str      # host-native roots, ';' or newline separated
    dv_detection: bool
    dv_file_tagging: bool
    dv_label_vocab: str        # JSON: {layer: label}

    # Debug & Logging
    debug_mode: bool
    clear_logs_startup: bool
    scan_threads: int

    # Matching thresholds (used in validation)
    tv_match_threshold: int
    low_match_threshold: int
    movie_match_threshold: int
    year_tolerance: int

    # Scanner
    base_url: str                   # base URL for HDEncode/source scraping
    scheduler_only_when_idle: bool  # only run scheduled scans when user is idle

    # Debug & Logging (also in _DEFAULT_CONFIG)
    verbose_logging: bool

    # Display
    tile_columns: int

    # Appearance
    theme_mode: Literal["dark", "light", "system"]

    # System Tray & Startup
    enable_system_tray: bool
    minimize_to_tray: bool
    start_minimized: bool
    auto_connect_plex: bool

    # Plex Account (remote) — uses plex_connection_mode (defined above)
    plex_selected_server: str

    # Auto-Grab
    auto_grab_enabled: bool
    auto_grab_min_rating: float
    auto_grab_min_votes: int
    auto_grab_genres: str           # comma-separated include list (empty = all)
    auto_grab_exclude_genres: str   # comma-separated exclude list
    auto_grab_languages: str        # comma-separated include list (empty = all)
    auto_grab_statuses: str         # comma-separated: "missing,upgrade,dv_upgrade"

    # Notifications
    desktop_notifications: bool
    discord_webhook: str
    discord_username: str
    slack_webhook: str
    pushover_user: str
    pushover_token: str
    webhook_url: str
    webhook_method: Literal["POST", "GET", "PUT"]
    email_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    email_from: str
    email_to: str
    smtp_tls: bool


# File paths - resolved relative to the project root (parent of backend/)
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE = os.path.join(_BASE_DIR, "download_history.json")  # Legacy file to migrate


# Data directory — databases live outside cloud-synced folders to avoid
# OneDrive/Dropbox corrupting SQLite WAL files during sync.
# Windows: %LOCALAPPDATA%\ScanHound\   Linux/macOS: ~/.local/share/scanhound/
def _try_migrate_dir(old_dir: str, new_dir: str) -> None:
    """Attempt one-time directory migration; log and continue on failure."""
    if not os.path.exists(new_dir) and os.path.exists(old_dir):
        import shutil
        try:
            shutil.move(old_dir, new_dir)
            if not os.path.isdir(new_dir):
                import sys
                print(f"[ScanHound] Migration incomplete — {new_dir} not found after move",
                      file=sys.stderr)
        except OSError as e:
            import sys
            print(f"[ScanHound] Migration failed ({old_dir} → {new_dir}): {e}",
                  file=sys.stderr)


def _get_data_dir() -> str:
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
        new_dir = os.path.join(base, 'ScanHound')
        _try_migrate_dir(os.path.join(base, 'MediaScout'), new_dir)
        return new_dir
    new_dir = os.path.join(os.path.expanduser('~'), '.local', 'share', 'scanhound')
    _try_migrate_dir(os.path.join(os.path.expanduser('~'), '.local', 'share', 'mediascout'), new_dir)
    return new_dir


_DATA_DIR = _get_data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(_DATA_DIR, "scanner.log")


def _migrate_db(name: str) -> str:
    """Return the data-dir path for a database, migrating from project root if needed."""
    new_path = os.path.join(_DATA_DIR, name)
    if not os.path.exists(new_path):
        old_path = os.path.join(_BASE_DIR, name)
        if os.path.exists(old_path):
            import shutil
            try:
                shutil.move(old_path, new_path)
                # Also move WAL/SHM sidecars if present
                for suffix in ('-wal', '-shm'):
                    old_sc = old_path + suffix
                    if os.path.exists(old_sc):
                        shutil.move(old_sc, new_path + suffix)
            except OSError as e:
                import sys
                print(f"[ScanHound] DB migration failed ({old_path} → {new_path}): {e}",
                      file=sys.stderr)
    return new_path


DB_PATH = _migrate_db("crawler.db")
CACHE_FILE = DB_PATH  # backwards-compat alias

# Config is stored outside the project directory to avoid leaking credentials
# via cloud sync (OneDrive, Dropbox, etc.) or accidental git commits.
# Windows: %APPDATA%\ScanHound\config.json
# Linux/macOS: ~/.config/scanhound/config.json
def _get_config_dir() -> str:
    if os.name == 'nt':
        base = os.environ.get('APPDATA', os.path.expanduser('~'))
        new_dir = os.path.join(base, 'ScanHound')
        _try_migrate_dir(os.path.join(base, 'MediaScout'), new_dir)
        return new_dir
    new_dir = os.path.join(os.path.expanduser('~'), '.config', 'scanhound')
    _try_migrate_dir(os.path.join(os.path.expanduser('~'), '.config', 'mediascout'), new_dir)
    return new_dir

_CONFIG_DIR = _get_config_dir()
CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
# Legacy path — used only for one-time migration in load_config()
_LEGACY_CONFIG_FILE = os.path.join(_BASE_DIR, "config.json")

# UI Constants
TOOLTIP_DELAY_MS = 500
DEFAULT_BUTTON_WIDTH = 120
DEFAULT_WINDOW_WIDTH_PERCENT = 0.9
DEFAULT_WINDOW_HEIGHT_PERCENT = 0.9
PROGRESS_UPDATE_THROTTLE_MS = 100

# Network Constants
API_RATE_LIMIT_DELAY = 0.25
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 2

# Default configuration values (treated as immutable - use get_default_config() to get a copy)
_DEFAULT_CONFIG: AppConfig = {
    "plex_url": "http://127.0.0.1:32400",
    "plex_token": "",
    "plex_server_id": "",
    "plex_connection_mode": "direct",
    "plex_username": "",
    "plex_password": "",
    "plex_server_name": "",
    "tmdb_api_key": "",
    "omdb_api_key": "",
    "use_tmdb": True,
    "min_size_mb": 200,
    "pref_res": "Prefer 4K",
    "show_rating": True,
    "show_votes": True,
    "show_rt": True,
    "show_rg": True,
    "show_nf": True,
    "show_links": True,
    "show_genres": True,
    "cache_duration": 4,
    "plex_refresh_mode": "auto",
    "plex_invalidate_on_new_content": True,
    "ignore_keywords": "Cam, TS, HC, KORSUB, TC",
    "upgrade_sensitivity": 10,
    "movie_libs": ["Movies (1080p)", "Movies (4K HDR)"],
    "tv_libs": ["TV Shows"],
    "known_libraries": [],
    "jd_enabled": False,
    "jd_method": "folder",
    "jd_folder": "",
    "jd_movies_folder": "",
    "jd_tv_folder": "",
    "jd_email": "",
    "jd_password": "",
    "jd_device": "",
    "rule_1080_4k": True,
    "rule_1080_4k_size": False,
    "rule_1080_1080": True,
    "rule_4k_4k": True,
    "rule_dv": True,
    "strict_resolution": False,
    "debug_mode": False,
    "verbose_logging": False,
    "exclude_720p": False,
    "source_2160p": True,
    "source_remux": True,
    "source_tv_packs": False,
    "ddlbase_enabled": True,
    "ddlbase_manual_resolution_timeout": 60,
    "cuty_email": "",
    "cuty_password": "",
    "adithd_enabled": True,
    "adithd_username": "",
    "adithd_password": "",
    "adithd_auto_reply": False,
    "adithd_preferred_host": "rapidgator",
    "scheduler_enabled": False,
    "scheduler_interval": 24,
    "last_scan_time": 0,
    "background_scan_enabled": False,
    "background_scan_interval_hours": 6,
    "background_scan_pages": 3,
    "background_scan_sources": ["HDEncode", "DDLBase", "Adit-HD"],
    "background_scan_retain_days": 7,
    "background_scan_last_run": 0,
    "auto_rename_enabled": False,
    "auto_rename_confidence_threshold": 70,
    "auto_rename_require_confirmation": True,
    "auto_rename_move_method": "hardlink",
    "auto_rename_movie_library": "",
    "auto_rename_movie_library_4k": "",
    "auto_rename_tv_library": "",
    "auto_rename_template_movie": "",
    "auto_rename_template_tv": "",
    "auto_rename_plex_sort_titles": False,
    "auto_rename_llm_enabled": False,
    "ollama_base_url": "http://ollama:11434",
    "ollama_model": "",
    "dv_library_roots": "",
    "dv_detection": False,
    "dv_file_tagging": False,
    "dv_label_vocab": '{"fel": "DV FEL", "mel": "DV MEL", "profile8": "DV P8", "profile5": "DV P5"}',
    "clear_logs_startup": False,
    "scan_threads": 10,
    "tv_match_threshold": 90,
    "low_match_threshold": 75,
    "movie_match_threshold": 85,
    "year_tolerance": 1,
    "base_url": "https://hdencode.org",
    "scheduler_only_when_idle": False,
    "tile_columns": 0,  # 0 = responsive auto-fill (sized by per-device tile size)
    "theme_mode": "dark",
    "enable_system_tray": False,
    "minimize_to_tray": False,
    "start_minimized": False,
    "auto_connect_plex": True,
    # plex_mode removed — use plex_connection_mode instead
    "plex_selected_server": "",
    "auto_grab_enabled": False,
    "auto_grab_min_rating": 0.0,
    "auto_grab_min_votes": 0,
    "auto_grab_genres": "",
    "auto_grab_exclude_genres": "",
    "auto_grab_languages": "",
    "auto_grab_statuses": "missing,upgrade,dv_upgrade",
    "desktop_notifications": True,
    "discord_webhook": "",
    "discord_username": "ScanHound",
    "slack_webhook": "",
    "pushover_user": "",
    "pushover_token": "",
    "webhook_url": "",
    "webhook_method": "POST",
    "email_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_password": "",
    "email_from": "",
    "email_to": "",
    "smtp_tls": True,
}

# Backwards-compatible alias (callers should prefer get_default_config())
DEFAULT_CONFIG = _DEFAULT_CONFIG


def get_default_config() -> AppConfig:
    """Return a deep copy of the default configuration to prevent mutation."""
    return copy.deepcopy(_DEFAULT_CONFIG)


def _safe_int(val, default):
    """Safely convert a value to int, returning default on failure."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_numeric(val, default):
    """Safely get a numeric value, returning default on failure."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def validate_config(config: dict) -> dict:
    """Validate and sanitize configuration values. Returns cleaned config."""
    cleaned = dict(config)

    # Numeric constraints (safe against None and non-numeric strings)
    if _safe_numeric(cleaned.get('min_size_mb'), 0) < 0:
        cleaned['min_size_mb'] = 0
    if _safe_numeric(cleaned.get('scheduler_interval'), 1) < 1:
        cleaned['scheduler_interval'] = 1
    scan_threads = cleaned.get('scan_threads')
    if scan_threads is not None:
        cleaned['scan_threads'] = max(1, min(50, _safe_int(scan_threads, 4)))
    if _safe_numeric(cleaned.get('cache_duration'), 1) < 0:
        cleaned['cache_duration'] = 0
    if _safe_numeric(cleaned.get('upgrade_sensitivity'), 0) < 0:
        cleaned['upgrade_sensitivity'] = 0

    if cleaned.get('plex_refresh_mode') not in (None, "auto", "force_refresh", "cache_only"):
        cleaned['plex_refresh_mode'] = "auto"

    # Threshold bounds (0-100)
    for key in ('tv_match_threshold', 'low_match_threshold', 'movie_match_threshold'):
        val = cleaned.get(key)
        if val is not None:
            cleaned[key] = max(0, min(100, _safe_int(val, 85)))

    if cleaned.get('year_tolerance') is not None:
        cleaned['year_tolerance'] = max(0, min(10, _safe_int(cleaned['year_tolerance'], 1)))

    # Auto-Grab constraints
    if cleaned.get('auto_grab_min_rating') is not None:
        cleaned['auto_grab_min_rating'] = max(0.0, min(10.0, _safe_numeric(cleaned['auto_grab_min_rating'], 0.0)))
    if cleaned.get('auto_grab_min_votes') is not None:
        cleaned['auto_grab_min_votes'] = max(0, _safe_int(cleaned['auto_grab_min_votes'], 0))

    return cleaned


# Settings Presets - Quick configurations for common use cases
SETTINGS_PRESETS = {
    "Aggressive Upgrades": {
        "description": "Flag all possible upgrades (4K, DV, size)",
        "rule_dv": True,
        "rule_1080_4k": True,
        "rule_1080_4k_size": True,
        "rule_1080_1080": True,
        "rule_4k_4k": True,
        "strict_resolution": False,
        "upgrade_sensitivity": 1,  # 1% size difference triggers upgrade
        "min_size_mb": 200
    },
    "Conservative": {
        "description": "Only show clear upgrades (4K and DV only)",
        "rule_dv": True,
        "rule_1080_4k": True,
        "rule_1080_4k_size": False,
        "rule_1080_1080": False,
        "rule_4k_4k": False,
        "strict_resolution": True,
        "upgrade_sensitivity": 10,  # 10% size difference required
        "min_size_mb": 500
    },
    "4K Only": {
        "description": "Only flag 4K content, ignore 1080p and size upgrades",
        "rule_dv": True,
        "rule_1080_4k": True,
        "rule_1080_4k_size": False,
        "rule_1080_1080": False,
        "rule_4k_4k": True,
        "strict_resolution": True,
        "upgrade_sensitivity": 5,
        "min_size_mb": 1000,
        "pref_res": "Prefer 4K"
    },
    "Quality Seeker": {
        "description": "Focus on quality: DV, HDR, and large file sizes",
        "rule_dv": True,
        "rule_1080_4k": True,
        "rule_1080_4k_size": True,
        "rule_1080_1080": True,
        "rule_4k_4k": True,
        "strict_resolution": False,
        "upgrade_sensitivity": 15,  # 15% larger = quality upgrade
        "min_size_mb": 2000
    },
    "Balanced": {
        "description": "Balanced settings (default configuration)",
        "rule_dv": True,
        "rule_1080_4k": True,
        "rule_1080_4k_size": False,
        "rule_1080_1080": True,
        "rule_4k_4k": True,
        "strict_resolution": False,
        "upgrade_sensitivity": 10,
        "min_size_mb": 200
    }
}
