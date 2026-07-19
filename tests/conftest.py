"""Shared test fixtures for ScanHound test suite."""

import os
import sys
import tempfile
import sqlite3
import pytest

# Ensure the project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Isolate config/data dirs from the user's REAL ones ──────────────────
# backend.config derives CONFIG_FILE from the APPDATA/LOCALAPPDATA env vars at
# import time. Redirect them to throwaway temp dirs *before* backend is imported
# so the test suite can never read or overwrite the user's real config.json
# (Plex token, API keys, notification URLs, etc.). This runs at conftest import,
# which pytest loads before any test module imports backend.
_TEST_APPDATA = tempfile.mkdtemp(prefix="scanhound_test_appdata_")
_TEST_LOCALAPPDATA = tempfile.mkdtemp(prefix="scanhound_test_localappdata_")
os.environ["APPDATA"] = _TEST_APPDATA
os.environ["LOCALAPPDATA"] = _TEST_LOCALAPPDATA
# Belt-and-suspenders: if backend.config/app_service were already imported, their
# CONFIG_FILE was computed from the real APPDATA — override it on those modules.
_TEST_CFG_DIR = os.path.join(_TEST_APPDATA, "ScanHound")
os.makedirs(_TEST_CFG_DIR, exist_ok=True)
for _modname in ("backend.config", "backend.app_service"):
    _mod = sys.modules.get(_modname)
    if _mod is not None:
        if hasattr(_mod, "CONFIG_FILE"):
            _mod.CONFIG_FILE = os.path.join(_TEST_CFG_DIR, "config.json")
        if hasattr(_mod, "_LEGACY_CONFIG_FILE"):
            _mod._LEGACY_CONFIG_FILE = os.path.join(_TEST_CFG_DIR, "legacy_config.json")


@pytest.fixture(autouse=True)
def _default_to_open_auth(monkeypatch):
    """Most of the test suite predates the auth system entirely and hits API
    routes with no password/nonce configured, expecting them reachable (the
    historical default). Wave A (backend/api/main.py) changed that default to
    fail-CLOSED when no credential exists, so a corrupted/reset DB can't
    silently reopen a real deployment. Tests that specifically exercise the
    fail-closed posture / escape hatch (tests/test_api_auth.py) override this
    per-test via monkeypatch.setenv/delenv, which layers on top of (and wins
    over) this autouse default within that test's scope.
    """
    monkeypatch.setenv("SCANHOUND_ALLOW_OPEN", "1")
    yield


@pytest.fixture(autouse=True)
def _isolate_config_file(tmp_path, monkeypatch):
    """Redirect the app config file to a temp path for EVERY test.

    Without this, tests that exercise the settings route / AppService.save_config
    (e.g. test_api_routes PUT /settings) write test fixture values to the user's
    real %APPDATA%/ScanHound/config.json — clobbering their Plex token, API keys,
    and notification URLs. Patch the module-level constants AppService reads so
    no test can ever touch the real config.
    """
    import backend.app_service as _app_service
    monkeypatch.setattr(_app_service, "CONFIG_FILE", str(tmp_path / "config.json"), raising=False)
    monkeypatch.setattr(_app_service, "_LEGACY_CONFIG_FILE", str(tmp_path / "legacy_config.json"), raising=False)
    # AppService.save_config() also exports the DV host subset to DV_HOST_JSON
    # (default /data/dv_host.json, which resolves to a drive-root path on
    # Windows). Without patching this, the full suite would attempt to write
    # to that uncontrolled path on every save_config() call.
    monkeypatch.setattr(_app_service, "DV_HOST_JSON", str(tmp_path / "dv_host.json"), raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_default_database(tmp_path, monkeypatch):
    """Give every test its own default crawler.db.

    backend.config and DatabaseManager bind their default database path at
    import time, so changing SCANHOUND_DB_DIR inside a test is not sufficient.
    Patch the class constructor for omitted-path calls while preserving every
    explicit db_path supplied by focused database tests.
    """
    import backend.config as _config
    import backend.database as _database

    isolated_dir = str(tmp_path)
    isolated_path = str(tmp_path / "crawler.db")
    omitted = object()
    original_init = _database.DatabaseManager.__init__

    def _isolated_init(self, db_path=omitted):
        resolved_path = isolated_path if db_path is omitted else db_path
        original_init(self, db_path=resolved_path)

    monkeypatch.setenv("SCANHOUND_DB_DIR", isolated_dir)
    monkeypatch.setattr(_config, "_DB_DIR", isolated_dir, raising=False)
    monkeypatch.setattr(_config, "DB_PATH", isolated_path, raising=False)
    monkeypatch.setattr(_config, "CACHE_FILE", isolated_path, raising=False)
    monkeypatch.setattr(_database, "DB_PATH", isolated_path, raising=False)
    monkeypatch.setattr(_database.DatabaseManager, "__init__", _isolated_init)

    # AppService imports CACHE_FILE by value. Keep its legacy persistence alias
    # inside the same per-test directory when that module is already loaded.
    app_service = sys.modules.get("backend.app_service")
    if app_service is not None:
        monkeypatch.setattr(
            app_service,
            "CACHE_FILE",
            isolated_path,
            raising=False,
        )

    yield isolated_path

    # TestClient normally closes this during lifespan shutdown. This final guard
    # prevents a global registry reference from retaining an open handle after a
    # test that constructed an app without entering its context manager.
    dependencies = sys.modules.get("backend.api.dependencies")
    registry = getattr(dependencies, "registry", None) if dependencies else None
    active_db = getattr(registry, "db", None) if registry else None
    if getattr(active_db, "db_path", None) == isolated_path:
        try:
            active_db.close()
        except Exception:
            pass
        registry.db = None


def test_default_database_path_is_function_scoped(_isolate_default_database):
    """Omitted-path DatabaseManagers must use this test's isolated crawler.db."""
    from backend.database import DatabaseManager

    first = DatabaseManager()
    second = DatabaseManager()
    try:
        assert first.db_path == _isolate_default_database
        assert second.db_path == _isolate_default_database
    finally:
        first.close()
        second.close()


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary database path that gets cleaned up."""
    db_path = str(tmp_path / "test_crawler.db")
    yield db_path


@pytest.fixture
def db_manager(tmp_db):
    """Provide an initialized DatabaseManager with a temp database."""
    from backend.database import DatabaseManager
    dm = DatabaseManager(db_path=tmp_db)
    yield dm
    dm.close()


@pytest.fixture
def default_config():
    """Provide a fresh copy of the default configuration."""
    from backend.config import get_default_config
    return get_default_config()


class MockApp:
    """Minimal mock of the parent app object used by MatchingEngine and scrapers."""

    # Status constants (from app_service.py)
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

    EMOJI_DV = "DV"
    EMOJI_4K = "4K"
    EMOJI_INFO = "i"
    EMOJI_WARNING = "!"

    def __init__(self, config=None):
        from backend.config import get_default_config
        self.config = config or get_default_config()
        self.download_history = set()
        self.tmdb_cache = {}
        self._logs = []

    def clean_string(self, s):
        """Normalize title string for matching."""
        import re
        if not s:
            return ""
        normalized = s.lower().strip()
        normalized = re.sub(r'\((\d{4})\)', '', normalized)
        normalized = re.sub(r'\b(19|20)\d{2}\b', '', normalized)
        normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def parse_size(self, s):
        """Parse size string to float GB."""
        import re
        try:
            if not s or not isinstance(s, str) or s == "?":
                return 0.0
            s_clean = str(s).upper().replace(' ', '')
            if 'TB' in s_clean or 'TIB' in s_clean:
                return float(re.sub(r'[A-Z]+', '', s_clean)) * 1024
            elif 'GB' in s_clean or 'GIB' in s_clean:
                return float(re.sub(r'[A-Z]+', '', s_clean))
            elif 'MB' in s_clean or 'MIB' in s_clean:
                return float(re.sub(r'[A-Z]+', '', s_clean)) / 1024
            return float(re.sub(r'[A-Z]+', '', s_clean))
        except (ValueError, TypeError):
            return 0.0

    def safe_log(self, msg):
        self._logs.append(msg)


@pytest.fixture
def mock_app():
    """Provide a MockApp instance."""
    return MockApp()


@pytest.fixture
def matching_engine(mock_app):
    """Provide a MatchingEngine with a MockApp."""
    from backend.matching import MatchingEngine
    return MatchingEngine(mock_app)


@pytest.fixture
def plex_index():
    """Provide a sample Plex index for testing."""
    movies = [
        {
            'clean_title': 'the matrix',
            'original_title': 'The Matrix',
            'year': 1999,
            'res': '1080p',
            'size': 15.0,
            'dovi': False,
            'hdr': False,
            'imdb_id': 'tt0133093',
            'rating_key': '1001',
            'media_id': 'm1001',
        },
        {
            'clean_title': 'inception',
            'original_title': 'Inception',
            'year': 2010,
            'res': '4K',
            'size': 55.0,
            'dovi': True,
            'hdr': True,
            'imdb_id': 'tt1375666',
            'rating_key': '1002',
            'media_id': 'm1002',
        },
        {
            'clean_title': 'the dark knight',
            'original_title': 'The Dark Knight',
            'year': 2008,
            'res': '1080p',
            'size': 12.0,
            'dovi': False,
            'hdr': False,
            'imdb_id': 'tt0468569',
            'rating_key': '1003',
            'media_id': 'm1003',
        },
        {
            'clean_title': 'interstellar',
            'original_title': 'Interstellar',
            'year': 2014,
            'res': '4K',
            'size': 65.0,
            'dovi': False,
            'hdr': True,
            'imdb_id': 'tt0816692',
            'rating_key': '1004',
            'media_id': 'm1004',
        },
    ]

    tv_shows = [
        {
            'clean_title': 'breaking bad',
            'original_title': 'Breaking Bad',
            'year': 2008,
            'res': '1080p',
            'size': 45.0,
            'dovi': False,
            'hdr': False,
            'imdb_id': 'tt0903747',
            'rating_key': '2001',
            'season': 1,
            'episode_count': 7,
            'is_tv': True,
        },
        {
            'clean_title': 'breaking bad',
            'original_title': 'Breaking Bad',
            'year': 2008,
            'res': '1080p',
            'size': 50.0,
            'dovi': False,
            'hdr': False,
            'imdb_id': 'tt0903747',
            'rating_key': '2002',
            'season': 2,
            'episode_count': 13,
            'is_tv': True,
        },
    ]

    all_items = movies + tv_shows

    by_imdb = {}
    for item in all_items:
        imdb = item.get('imdb_id')
        if imdb:
            by_imdb.setdefault(imdb, []).append(item)

    by_title = {}
    for item in all_items:
        title = item.get('clean_title', '').lower()
        if title:
            by_title.setdefault(title, []).append(item)

    return {
        "by_imdb": by_imdb,
        "by_title": by_title,
        "all_items": all_items,
    }
