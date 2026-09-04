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
def _isolate_runtime_writer_lock_state():
    import backend.runtime_lock as runtime_lock_module

    def cleanup():
        with runtime_lock_module._STATE_LOCK:
            locks = list(runtime_lock_module._ACTIVE_LOCKS.values())
        for lock in locks:
            lock.release()
        with runtime_lock_module._STATE_LOCK:
            runtime_lock_module._ACTIVE_LOCKS.clear()
            runtime_lock_module._TEST_BYPASS_DEPTH = 0

    cleanup()
    try:
        with runtime_lock_module._unlocked_fileops_for_tests():
            yield
    finally:
        cleanup()


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
def _isolate_trash_root_registry(tmp_path, monkeypatch):
    """Never let file-operation tests touch the user's persistent root index."""
    from backend.rename import fileops as _fileops

    monkeypatch.setattr(
        _fileops,
        "_TRASH_ROOTS_INDEX",
        str(tmp_path / "trash_roots.json"),
        raising=False,
    )
    monkeypatch.setattr(
        _fileops,
        "_TRASH_ROOTS_RUNTIME",
        set(),
        raising=False,
    )
    yield


# ── TST-1: the suite must never write into a REAL volume trash root ─────────
# fileops._trash_root_for() sites a disposal's bucket at the root of the
# SOURCE's own volume: <drive>:\.scanhound-trash on Windows, <mount>/.scanhound-trash
# on POSIX. A test that trashes a file under tmp_path therefore wrote into the
# host's real C:\.scanhound-trash (400 buckets between 2026-08-03 and 09-03),
# changed later tests' outcomes, and produced "flakes" that were pollution
# (round-7 review, TST-1). Three pieces:
#   * _isolate_volume_trash_root redirects BOTH roots (the per-volume root and
#     the app-data fallback) into tmp_path for every test;
#   * the same fixture snapshots every real volume root on this host before the
#     test and fails, naming the test, if any of them changed;
#   * _host_trash_roots_untouched_for_the_session does the same for the whole
#     session, so writes outside a test's own scope are caught too.
# The redirect also pins _same_volume_trash_roots to the redirected root:
# the real walk appends "<ancestor>/.scanhound-trash" for EVERY ancestor of the
# source up to the volume root, and _trash creates the first writable one when
# the primary fails, so a source outside tmp_path could otherwise reach a real
# drive root through a fallback the guard does not watch.
# A test that needs the REAL derivation, and writes nothing, opts out of the
# redirect with @pytest.mark.real_trash_root; the guard still applies to it.
# The guard reports; it never deletes anything from a real root.

_REAL_TRASH_ROOT_FOR = None   # the unpatched derivation, captured once per session
_REAL_VOLUME_TRASH_ROOTS = None


def _real_volume_trash_roots():
    """Every volume-root trash candidate on THIS host, by the unpatched derivation."""
    global _REAL_VOLUME_TRASH_ROOTS
    # Computed ONCE, at session start, and this is load-bearing: some tests
    # monkeypatch os.name to "posix" or os.stat to raise, and those patches are
    # still live when the per-test guard's teardown runs. The teardown only
    # calls os.scandir + DirEntry.stat on the cached list, which neither patch
    # reaches.
    if _REAL_VOLUME_TRASH_ROOTS is None:
        from backend.rename import fileops as _fileops
        assert _REAL_TRASH_ROOT_FOR is not None, (
            "the session guard must capture the real derivation first"
        )
        real = _REAL_TRASH_ROOT_FOR
        roots = set()
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.isdir(drive):
                    roots.add(real(drive))
        else:
            for mp in _fileops._posix_mount_points():
                roots.add(os.path.join(mp, ".scanhound-trash"))
            roots.add(real("/"))
        _REAL_VOLUME_TRASH_ROOTS = sorted(roots)
    return _REAL_VOLUME_TRASH_ROOTS


def _snapshot_trash_roots(roots):
    """{root: {entry: content} | None (absent) | "unreadable"}.

    ``content`` is the sorted tuple of names inside a bucket, or the size of a
    loose file. One level INSIDE each bucket is deliberate: a bucket's own
    mtime does NOT move on this Windows host when a file is added inside it
    (measured while writing this), so mtime is not a detector. Two trash
    disposals in the same second share one bucket, so a write into an existing
    bucket is a real case, not a corner. Cost: one scandir per root plus one
    per bucket; a root holding hundreds of stale buckets is the expensive
    case, which is one more reason to clear them.
    """
    snap = {}
    for root in roots:
        try:
            with os.scandir(root) as it:
                entries = {}
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        try:
                            entries[e.name] = tuple(sorted(os.listdir(e.path)))
                        except OSError:
                            entries[e.name] = "unreadable"
                    else:
                        try:
                            entries[e.name] = e.stat(follow_symlinks=False).st_size
                        except OSError:
                            entries[e.name] = "unreadable"
                snap[root] = entries
        except FileNotFoundError:
            snap[root] = None
        except OSError:
            snap[root] = "unreadable"
    return snap


def _describe_trash_root_changes(before, after):
    """Human-readable diff of two snapshots; empty string when nothing changed."""
    lines = []
    for root in sorted(set(before) | set(after)):
        b, a = before.get(root), after.get(root)
        if b == a:
            continue
        if isinstance(b, dict) and isinstance(a, dict):
            added = sorted(set(a) - set(b))
            removed = sorted(set(b) - set(a))
            changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
            lines.append(
                f"{root}: added={added[:5]} removed={removed[:5]} modified={changed[:5]}"
            )
        else:
            lines.append(f"{root}: {b!r} -> {a!r}")
    return "\n".join(lines)


@pytest.fixture(scope="session", autouse=True)
def _host_trash_roots_untouched_for_the_session():
    global _REAL_TRASH_ROOT_FOR
    from backend.rename import fileops as _fileops
    _REAL_TRASH_ROOT_FOR = _fileops._trash_root_for
    roots = _real_volume_trash_roots()
    before = _snapshot_trash_roots(roots)
    yield
    diff = _describe_trash_root_changes(before, _snapshot_trash_roots(roots))
    if diff:
        pytest.fail(
            "TST-1: the session changed a REAL volume trash root. Nothing was "
            "deleted; inspect and remove the entries by hand:\n" + diff,
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _isolate_volume_trash_root(request, tmp_path, monkeypatch):
    from backend.rename import fileops as _fileops
    roots = _real_volume_trash_roots()
    before = _snapshot_trash_roots(roots)
    if request.node.get_closest_marker("real_trash_root") is None:
        volume_root = str(tmp_path / ".scanhound-trash")
        monkeypatch.setattr(
            _fileops, "_trash_root_for", lambda _path, _root=volume_root: _root
        )
        def _pinned_same_volume_roots(path):
            # Same contract as the real walk, minus the ancestors: nothing when
            # the derived root is the app-data fallback, else that root alone.
            # Read at call time so a test's own patch of either name wins.
            primary = _fileops._trash_root_for(path)
            return [] if primary == _fileops._TRASH_ROOT else [primary]

        monkeypatch.setattr(
            _fileops, "_same_volume_trash_roots", _pinned_same_volume_roots
        )
        monkeypatch.setattr(_fileops, "_TRASH_ROOT", str(tmp_path / "appdata-trash"))
    yield
    diff = _describe_trash_root_changes(before, _snapshot_trash_roots(roots))
    if diff:
        pytest.fail(
            f"TST-1: {request.node.nodeid} changed a REAL volume trash root. "
            "Nothing was deleted; inspect and remove the entries by hand:\n" + diff,
            pytrace=False,
        )


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
