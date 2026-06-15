"""SettingsController — Config read/write, test buttons, presets for QML."""

import logging
import requests
import threading
from PySide6.QtCore import QObject, QMetaObject, Qt, Signal, Slot, Property, Q_ARG

from backend.app_service import AppService

logger = logging.getLogger(__name__)


class SettingsController(QObject):
    """Controls the Settings dialog — exposes config values and test functions."""

    # Signals for property changes
    configChanged = Signal()
    testResultChanged = Signal(str, bool, str)  # (name, success, message)

    # Plex test
    plexTestStatusChanged = Signal()
    plexAccountResult = Signal(bool, str)  # success, message
    librariesChanged = Signal()

    # API test
    tmdbTestStatusChanged = Signal()
    omdbTestStatusChanged = Signal()

    # JDownloader test
    jdTestStatusChanged = Signal()

    # Adit-HD test
    adithdTestStatusChanged = Signal()

    # Cuty.io test
    cutyTestStatusChanged = Signal()

    def __init__(self, backend: AppService, parent=None):
        super().__init__(parent)
        self._backend = backend

        # Test statuses: "untested" | "testing" | "success" | "error"
        self._plex_test_status = "untested"
        self._plex_test_message = ""
        self._tmdb_test_status = "untested"
        self._tmdb_test_message = ""
        self._omdb_test_status = "untested"
        self._omdb_test_message = ""
        self._jd_test_status = "untested"
        self._jd_test_message = ""
        self._adithd_test_status = "untested"
        self._adithd_test_message = ""
        self._cuty_test_status = "untested"
        self._cuty_test_message = ""

        # Libraries fetched from Plex — guarded by _libraries_lock because
        # testPlex() and testPlexAccount() write from background threads.
        self._libraries = []  # [{name, type, assignment}]
        self._libraries_lock = threading.Lock()
        self._library_counts = {}  # {library_name: count}
        self._plex_servers = []
        self._plex_servers_lock = threading.Lock()

        # Pre-populate from stored config so assignments survive across sessions
        self._load_libraries_from_config()

    # ── Config access ──────────────────────────────────────────────

    @Slot(str, result=str)
    def getString(self, key):
        return str(self._backend.config.get(key, ""))

    @Slot(str, result=bool)
    def getBool(self, key):
        return bool(self._backend.config.get(key, False))

    @Slot(str, result=int)
    def getInt(self, key):
        try:
            return int(self._backend.config.get(key, 0))
        except (ValueError, TypeError):
            return 0

    @Slot(str, result=float)
    def getFloat(self, key):
        try:
            return float(self._backend.config.get(key, 0.0))
        except (ValueError, TypeError):
            return 0.0

    @Slot(str, str)
    def setString(self, key, value):
        self._backend.config[key] = value

    @Slot(str, bool)
    def setBool(self, key, value):
        self._backend.config[key] = value

    @Slot(str, int)
    def setInt(self, key, value):
        self._backend.config[key] = value

    @Slot(str, float)
    def setFloat(self, key, value):
        self._backend.config[key] = value

    # ── Save / Cancel ──────────────────────────────────────────────

    @Slot()
    def save(self):
        """Save all config changes to disk."""
        self._backend.save_config()
        logger.info("Settings saved")

    @Slot()
    def cancel(self):
        """Reload config from disk, discarding changes (in-place update)."""
        fresh = self._backend.load_config()
        self._backend.config.clear()
        self._backend.config.update(fresh)
        logger.info("Settings cancelled, config reloaded")
        self.configChanged.emit()

    # ── Test status properties ─────────────────────────────────────

    @Property(str, notify=plexTestStatusChanged)
    def plexTestStatus(self):
        return self._plex_test_status

    @Property(str, notify=plexTestStatusChanged)
    def plexTestMessage(self):
        return self._plex_test_message

    @Property(str, notify=tmdbTestStatusChanged)
    def tmdbTestStatus(self):
        return self._tmdb_test_status

    @Property(str, notify=tmdbTestStatusChanged)
    def tmdbTestMessage(self):
        return self._tmdb_test_message

    @Property(str, notify=omdbTestStatusChanged)
    def omdbTestStatus(self):
        return self._omdb_test_status

    @Property(str, notify=omdbTestStatusChanged)
    def omdbTestMessage(self):
        return self._omdb_test_message

    @Property(str, notify=jdTestStatusChanged)
    def jdTestStatus(self):
        return self._jd_test_status

    @Property(str, notify=jdTestStatusChanged)
    def jdTestMessage(self):
        return self._jd_test_message

    @Property(str, notify=adithdTestStatusChanged)
    def adithdTestStatus(self):
        return self._adithd_test_status

    @Property(str, notify=adithdTestStatusChanged)
    def adithdTestMessage(self):
        return self._adithd_test_message

    @Property(str, notify=cutyTestStatusChanged)
    def cutyTestStatus(self):
        return self._cuty_test_status

    @Property(str, notify=cutyTestStatusChanged)
    def cutyTestMessage(self):
        return self._cuty_test_message

    # ── Thread-safe helpers ─────────────────────────────────────────

    @Slot(str, str, str)
    def _onTestResult(self, testName, status, message):
        """Slot invoked on the main thread to update test results safely."""
        if testName == "plex":
            self._plex_test_status = status
            self._plex_test_message = message
            self.plexTestStatusChanged.emit()
        elif testName == "tmdb":
            self._tmdb_test_status = status
            self._tmdb_test_message = message
            self.tmdbTestStatusChanged.emit()
        elif testName == "omdb":
            self._omdb_test_status = status
            self._omdb_test_message = message
            self.omdbTestStatusChanged.emit()
        elif testName == "jd":
            self._jd_test_status = status
            self._jd_test_message = message
            self.jdTestStatusChanged.emit()
        elif testName == "adithd":
            self._adithd_test_status = status
            self._adithd_test_message = message
            self.adithdTestStatusChanged.emit()
        elif testName == "cuty":
            self._cuty_test_status = status
            self._cuty_test_message = message
            self.cutyTestStatusChanged.emit()

    @Slot()
    def _onLibrariesFetched(self):
        """Slot invoked on the main thread after Plex libraries are fetched."""
        self.librariesChanged.emit()

    def _emit_test_result(self, testName, status, message):
        """Thread-safe: schedule _onTestResult on the main thread."""
        QMetaObject.invokeMethod(
            self, "_onTestResult",
            Qt.QueuedConnection,
            Q_ARG(str, testName), Q_ARG(str, status), Q_ARG(str, message),
        )

    # ── Test buttons ───────────────────────────────────────────────

    def _load_libraries_from_config(self):
        """Pre-populate _libraries from plex_sections stored in config (no network call).

        Falls back to migrating from legacy known_movie_libraries / known_tv_libraries
        keys if plex_sections is absent, so users upgrading from older config versions
        don't lose their library assignments.
        """
        logger.info("Loading library assignments from config")
        sections = self._backend.config.get("plex_sections", [])

        if not sections:
            # Migration: seed from legacy keys if present
            known_movie = self._backend.config.get("known_movie_libraries", [])
            known_tv = self._backend.config.get("known_tv_libraries", [])
            known_all = self._backend.config.get("known_libraries", [])
            if known_movie or known_tv:
                assigned = set(known_movie) | set(known_tv)
                others = [n for n in known_all if n not in assigned]
                sections = (
                    [{"name": n, "type": "movie"} for n in known_movie]
                    + [{"name": n, "type": "show"} for n in known_tv]
                    + [{"name": n, "type": ""} for n in others]
                )
                # If movie_libs / tv_libs are empty, seed them from the legacy keys
                if not self._backend.config.get("movie_libs"):
                    self._backend.config["movie_libs"] = list(known_movie)
                if not self._backend.config.get("tv_libs"):
                    self._backend.config["tv_libs"] = list(known_tv)
                logger.info(
                    f"Migrated {len(sections)} Plex sections from legacy config keys"
                )

        if not sections:
            # Final fallback: reconstruct from movie_libs / tv_libs directly.
            # Handles configs where plex_sections was never written (e.g. user
            # saved library assignments without ever clicking Test Connection).
            movie_libs_cfg = self._backend.config.get("movie_libs", [])
            tv_libs_cfg = self._backend.config.get("tv_libs", [])
            if movie_libs_cfg or tv_libs_cfg:
                sections = (
                    [{"name": n, "type": "movie"} for n in movie_libs_cfg]
                    + [{"name": n, "type": "show"} for n in tv_libs_cfg]
                )
                logger.info(
                    f"Reconstructed {len(sections)} library sections from movie_libs/tv_libs"
                )

        if not sections:
            return

        movie_libs = self._backend.config.get("movie_libs", [])
        tv_libs = self._backend.config.get("tv_libs", [])
        self._libraries = [
            {
                "name": sec.get("name", ""),
                "type": sec.get("type", ""),
                "assignment": (
                    "movies" if sec.get("name", "") in movie_libs
                    else "tv" if sec.get("name", "") in tv_libs
                    else "none"
                ),
            }
            for sec in sections
            if sec.get("name")
        ]
        self._refresh_library_counts()

    def _refresh_library_counts(self):
        """Load per-library item counts from DB cache."""
        try:
            rows = self._backend.db.plex_cache_counts_per_library()
            self._library_counts = {r["library_name"]: r["count"] for r in rows}
        except Exception:
            self._library_counts = {}

    @Slot()
    def loadLibraries(self):
        """Reload library list from stored config without a network call."""
        self._load_libraries_from_config()
        self.librariesChanged.emit()

    @Slot()
    def testPlex(self):
        """Test Plex connection in background thread."""
        self._plex_test_status = "testing"
        self._plex_test_message = "Connecting..."
        self.plexTestStatusChanged.emit()

        def _test():
            try:
                url = self._backend.config.get("plex_url", "").rstrip("/")
                token = self._backend.config.get("plex_token", "")
                if not url or not token:
                    self._emit_test_result("plex", "error", "URL and token required")
                    return

                resp = requests.get(
                    f"{url}/library/sections",
                    headers={"X-Plex-Token": token, "Accept": "application/json"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    libs = data.get("MediaContainer", {}).get("Directory", [])
                    new_libraries = [
                        {"name": lib.get("title", ""),
                         "type": lib.get("type", ""),
                         "assignment": "none"}
                        for lib in libs
                    ]
                    # Pre-fill assignments from config
                    movie_libs = self._backend.config.get("movie_libs", [])
                    tv_libs = self._backend.config.get("tv_libs", [])
                    for lib in new_libraries:
                        if lib["name"] in movie_libs:
                            lib["assignment"] = "movies"
                        elif lib["name"] in tv_libs:
                            lib["assignment"] = "tv"
                    with self._libraries_lock:
                        self._libraries = new_libraries

                    # Persist discovered sections so future sessions can pre-populate
                    # without requiring Test Connection again
                    self._backend.config["plex_sections"] = [
                        {"name": lib["name"], "type": lib["type"]}
                        for lib in self._libraries
                    ]
                    self._backend.save_config()
                    logger.info(f"Saved {len(self._libraries)} Plex sections to config")

                    # Establish full PlexManager connection for scanner use
                    pm = self._backend.plex_manager
                    if not pm.is_connected:
                        conn_mode = self._backend.config.get("plex_connection_mode", "direct")
                        pm.configure(
                            url, token,
                            connection_mode=conn_mode,
                            username=self._backend.config.get("plex_username", ""),
                            password=self._backend.config.get("plex_password", ""),
                            server_name=self._backend.config.get("plex_server_name", ""),
                        )
                        pm_ok, pm_msg = pm.connect(timeout=15)
                        if pm_ok:
                            self._emit_test_result("plex", "success", f"Connected — {len(libs)} libraries")
                        else:
                            self._emit_test_result("plex", "success", f"API OK ({len(libs)} libs) — PlexManager: {pm_msg}")
                    else:
                        self._emit_test_result("plex", "success", f"Connected — {len(libs)} libraries")

                    QMetaObject.invokeMethod(self, "_onLibrariesFetched", Qt.QueuedConnection)
                else:
                    self._emit_test_result("plex", "error", f"HTTP {resp.status_code}")
            except Exception as e:
                self._emit_test_result("plex", "error", str(e)[:80])

        threading.Thread(target=_test, daemon=True).start()

    @Slot()
    def testTmdb(self):
        """Test TMDB API key."""
        self._tmdb_test_status = "testing"
        self._tmdb_test_message = "Testing..."
        self.tmdbTestStatusChanged.emit()

        def _test():
            try:
                key = self._backend.config.get("tmdb_api_key", "")
                if not key:
                    self._emit_test_result("tmdb", "error", "API key required")
                    return

                resp = requests.get(
                    f"https://api.themoviedb.org/3/configuration?api_key={key}",
                    timeout=10,
                )
                if resp.status_code == 200:
                    self._emit_test_result("tmdb", "success", "Valid API key")
                else:
                    self._emit_test_result("tmdb", "error", f"HTTP {resp.status_code}")
            except Exception as e:
                self._emit_test_result("tmdb", "error", str(e)[:80])

        threading.Thread(target=_test, daemon=True).start()

    @Slot()
    def testOmdb(self):
        """Test OMDb API key."""
        self._omdb_test_status = "testing"
        self._omdb_test_message = "Testing..."
        self.omdbTestStatusChanged.emit()

        def _test():
            try:
                key = self._backend.config.get("omdb_api_key", "")
                if not key:
                    self._emit_test_result("omdb", "error", "API key required")
                    return

                resp = requests.get(
                    f"https://www.omdbapi.com/?apikey={key}&t=Inception",
                    timeout=10,
                )
                data = resp.json()
                if data.get("Response") == "True":
                    self._emit_test_result("omdb", "success", "Valid API key")
                else:
                    self._emit_test_result("omdb", "error", data.get("Error", "Invalid key"))
            except Exception as e:
                self._emit_test_result("omdb", "error", str(e)[:80])

        threading.Thread(target=_test, daemon=True).start()

    @Slot()
    def testJd(self):
        """Test JDownloader / MyJDownloader connection."""
        self._jd_test_status = "testing"
        self._jd_test_message = "Connecting..."
        self.jdTestStatusChanged.emit()

        def _test():
            try:
                email = self._backend.config.get("jd_email", "")
                password = self._backend.config.get("jd_password", "")
                device_name = self._backend.config.get("jd_device", "")

                if not email or not password:
                    self._emit_test_result("jd", "error", "Email and password required")
                    return

                import myjdapi
                jd = myjdapi.Myjdapi()
                jd.connect(email, password)
                jd.update_devices()
                devices = jd.list_devices()

                if not devices:
                    self._emit_test_result("jd", "error", "Connected but no devices found")
                    return

                device_names = [d["name"] for d in devices]
                if device_name and device_name not in device_names:
                    self._emit_test_result(
                        "jd", "success",
                        f"Connected — {len(devices)} device(s), but '{device_name}' not found. Available: {', '.join(device_names)}"
                    )
                else:
                    target = device_name or device_names[0]
                    self._emit_test_result("jd", "success", f"Connected — device: {target}")

                jd.disconnect()
            except ImportError:
                self._emit_test_result("jd", "error", "myjdapi not installed (pip install myjdapi)")
            except Exception as e:
                self._emit_test_result("jd", "error", str(e)[:80])

        threading.Thread(target=_test, daemon=True).start()

    @Slot()
    def testAditHd(self):
        """Test Adit-HD forum connection by attempting HTTP login."""
        self._adithd_test_status = "testing"
        self._adithd_test_message = "Connecting..."
        self.adithdTestStatusChanged.emit()

        def _test():
            try:
                user = self._backend.config.get("adithd_username", "")
                passwd = self._backend.config.get("adithd_password", "")

                if not user or not passwd:
                    self._emit_test_result("adithd", "error", "Username and password required")
                    return

                session = requests.Session()
                # POST login form to MyBB forum
                resp = session.post(
                    "https://www.adit-hd.com/member.php",
                    data={
                        "action": "do_login",
                        "username": user,
                        "password": passwd,
                        "submit": "Login",
                        "url": "",
                    },
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"},
                    timeout=15,
                    allow_redirects=True,
                )
                # Check if login succeeded — MyBB sets mybbuser cookie on success
                if "mybbuser" in session.cookies.get_dict():
                    self._emit_test_result("adithd", "success", "Login successful")
                elif resp.status_code == 200 and "logout" in resp.text.lower():
                    self._emit_test_result("adithd", "success", "Login successful")
                elif resp.status_code == 200:
                    self._emit_test_result("adithd", "error", "Invalid username or password")
                else:
                    self._emit_test_result("adithd", "error", f"HTTP {resp.status_code}")
            except Exception as e:
                self._emit_test_result("adithd", "error", str(e)[:80])

        threading.Thread(target=_test, daemon=True).start()

    @Slot()
    def testCuty(self):
        """Test Cuty.io login via HTTP."""
        self._cuty_test_status = "testing"
        self._cuty_test_message = "Connecting..."
        self.cutyTestStatusChanged.emit()

        def _test():
            try:
                email = self._backend.config.get("cuty_email", "")
                password = self._backend.config.get("cuty_password", "")

                if not email or not password:
                    self._emit_test_result("cuty", "error", "Email and password required")
                    return

                session = requests.Session()
                # GET login page to obtain CSRF token
                login_page = session.get(
                    "https://cuty.io/login",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"},
                    timeout=15,
                )
                # Extract CSRF token from the page
                csrf_token = ""
                if '_token' in login_page.text:
                    import re
                    match = re.search(r'name="_token"\s+value="([^"]+)"', login_page.text)
                    if match:
                        csrf_token = match.group(1)

                # POST login
                data = {"email": email, "password": password}
                if csrf_token:
                    data["_token"] = csrf_token

                resp = session.post(
                    "https://cuty.io/login",
                    data=data,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                        "Referer": "https://cuty.io/login",
                    },
                    timeout=15,
                    allow_redirects=True,
                )

                # Check if login succeeded — typically redirects to dashboard
                final_url = resp.url
                page_text = resp.text.lower()
                if "dashboard" in final_url or "dashboard" in page_text:
                    self._emit_test_result("cuty", "success", "Login successful")
                elif "logout" in page_text:
                    self._emit_test_result("cuty", "success", "Login successful")
                elif resp.status_code == 200 and "login" in final_url:
                    self._emit_test_result("cuty", "error", "Invalid email or password")
                else:
                    self._emit_test_result("cuty", "error", f"Unexpected response (HTTP {resp.status_code})")
            except Exception as e:
                self._emit_test_result("cuty", "error", str(e)[:80])

        threading.Thread(target=_test, daemon=True).start()

    # ── Library management ─────────────────────────────────────────

    @Property(int, notify=librariesChanged)
    def libraryCount(self):
        with self._libraries_lock:
            return len(self._libraries)

    @Slot(int, result=str)
    def libraryName(self, index):
        with self._libraries_lock:
            if 0 <= index < len(self._libraries):
                return self._libraries[index]["name"]
        return ""

    @Slot(int, result=str)
    def libraryType(self, index):
        with self._libraries_lock:
            if 0 <= index < len(self._libraries):
                return self._libraries[index]["type"]
        return ""

    @Slot(int, result=str)
    def libraryAssignment(self, index):
        with self._libraries_lock:
            if 0 <= index < len(self._libraries):
                return self._libraries[index]["assignment"]
        return "none"

    @Slot(int, result=int)
    def libraryItemCount(self, index):
        """Return cached item count for a library from DB."""
        with self._libraries_lock:
            if 0 <= index < len(self._libraries):
                lib_name = self._libraries[index]["name"]
                return self._library_counts.get(lib_name, 0)
        return 0

    @Slot(int, str)
    def setLibraryAssignment(self, index, assignment):
        with self._libraries_lock:
            if 0 <= index < len(self._libraries):
                self._libraries[index]["assignment"] = assignment

    @Slot()
    def saveLibraryAssignments(self):
        """Save library assignments to config."""
        with self._libraries_lock:
            movie_libs = [lib["name"] for lib in self._libraries if lib["assignment"] == "movies"]
            tv_libs = [lib["name"] for lib in self._libraries if lib["assignment"] == "tv"]
        self._backend.config["movie_libs"] = movie_libs
        self._backend.config["tv_libs"] = tv_libs
        # Keep plex_sections up-to-date so it always survives across sessions
        if self._libraries:
            self._backend.config["plex_sections"] = [
                {"name": lib["name"], "type": lib["type"]}
                for lib in self._libraries
            ]

    # ── Presets ────────────────────────────────────────────────────

    @Slot(str)
    def applyPreset(self, presetName):
        if self._backend.apply_preset(presetName):
            self.configChanged.emit()

    # ── Log viewer support ─────────────────────────────────────────

    @Slot(result="QVariantList")
    def getLogHistory(self):
        """Return buffered log entries captured since app startup.

        Called by LogTab.qml on Component.onCompleted so the viewer is
        pre-populated with messages that arrived before the dialog opened.
        """
        buf = getattr(self._backend, "log_buffer", None)
        if buf is not None:
            return buf.get_entries()
        return []

    @Slot(bool)
    def setVerboseLogging(self, enabled: bool):
        """Toggle verbose (DEBUG-level) logging at runtime without a restart."""
        import logging as _logging
        self._backend.config["verbose_logging"] = enabled
        self._backend.config["debug_mode"] = enabled
        level = _logging.DEBUG if enabled else _logging.INFO
        root = _logging.getLogger()
        root.setLevel(level)
        for handler in root.handlers:
            handler.setLevel(level)
        logger.info(
            "Verbose logging %s (level set to %s)",
            "enabled" if enabled else "disabled",
            "DEBUG" if enabled else "INFO",
        )

    # ── Maintenance ────────────────────────────────────────────────

    @Slot()
    def purgeCache(self):
        """Clear persisted Plex cache and refresh visible library counts."""
        try:
            self._backend.db.clear_plex_cache()
            self._refresh_library_counts()
            self.librariesChanged.emit()
            logger.info("Plex cache purged")
        except Exception as e:
            logger.error(f"Failed to purge Plex cache: {e}")

    @Slot()
    def purgeMetadataCache(self):
        """Clear in-memory metadata caches used for enrichment."""
        self._backend.tmdb_cache.clear()
        self._backend.omdb_cache.clear()
        logger.info("Metadata caches purged")

    @Slot()
    def purgeHistory(self):
        """Clear download history."""
        try:
            with self._backend.db.transaction() as conn:
                if conn:
                    conn.execute("DELETE FROM downloads")
            logger.info("Download history purged")
        except Exception as e:
            logger.error(f"Failed to purge history: {e}")

    # ── Plex Account (Remote) ─────────────────────────────────────

    @Slot()
    def testPlexAccount(self):
        """Login to plex.tv and discover servers."""
        def _do():
            try:
                username = self._backend.config.get("plex_username", "")
                password = self._backend.config.get("plex_password", "")
                if not username or not password:
                    self._emit_plex_account_result(False, "Username and password required")
                    return

                from plexapi.myplex import MyPlexAccount
                account = MyPlexAccount(username, password)
                resources = account.resources()
                servers = [r for r in resources if r.provides == "server"]

                if not servers:
                    self._emit_plex_account_result(False, "No Plex servers found on account")
                    return

                with self._plex_servers_lock:
                    self._plex_servers = [s.name for s in servers]
                self._emit_plex_account_result(True, f"Found {len(servers)} server(s)")

            except ImportError:
                self._emit_plex_account_result(False, "plexapi not installed (pip install plexapi)")
            except Exception as e:
                self._emit_plex_account_result(False, str(e))

        threading.Thread(target=_do, name="plex-account-test", daemon=True).start()

    def _emit_plex_account_result(self, success: bool, message: str):
        QMetaObject.invokeMethod(
            self, "_onPlexAccountResult", Qt.QueuedConnection,
            Q_ARG(bool, success), Q_ARG(str, message),
        )

    @Slot(bool, str)
    def _onPlexAccountResult(self, success, message):
        self.plexAccountResult.emit(success, message)

    @Slot(result="QVariantList")
    def getPlexServers(self):
        return getattr(self, "_plex_servers", [])

    # ── Notification Tests ────────────────────────────────────────

    def _test_notification(self, channel: str, test_fn):
        """Generic notification test wrapper."""
        def _do():
            try:
                test_fn()
                self._emit_notif_test_result(channel, True, f"{channel} test sent successfully")
            except Exception as e:
                self._emit_notif_test_result(channel, False, str(e))

        threading.Thread(target=_do, name=f"test-{channel}", daemon=True).start()

    def _emit_notif_test_result(self, name: str, success: bool, message: str):
        QMetaObject.invokeMethod(
            self, "_onNotifTestResult", Qt.QueuedConnection,
            Q_ARG(str, name), Q_ARG(bool, success), Q_ARG(str, message),
        )

    @Slot(str, bool, str)
    def _onNotifTestResult(self, name, success, message):
        self.testResultChanged.emit(name, success, message)

    @Slot()
    def testDesktopNotification(self):
        """Send a test desktop notification."""
        def _do():
            try:
                from plyer import notification as plyer_notif
                plyer_notif.notify(
                    title="ScanHound Test",
                    message="Desktop notifications are working!",
                    timeout=5,
                )
                self._emit_notif_test_result("desktop", True, "Desktop notification sent")
            except ImportError:
                self._emit_notif_test_result("desktop", False, "plyer not installed")
            except Exception as e:
                self._emit_notif_test_result("desktop", False, str(e))

        threading.Thread(target=_do, name="test-desktop", daemon=True).start()

    @Slot()
    def testDiscordWebhook(self):
        """Send test message to Discord webhook."""
        url = self._backend.config.get("discord_webhook", "")
        username = self._backend.config.get("discord_username", "ScanHound")
        if not url:
            return

        def _do():
            try:
                resp = requests.post(url, json={
                    "username": username,
                    "content": "ScanHound test notification - Discord webhook is working!",
                }, timeout=10)
                if resp.status_code in (200, 204):
                    self._emit_notif_test_result("discord", True, "Discord test sent")
                else:
                    self._emit_notif_test_result("discord", False, f"HTTP {resp.status_code}")
            except Exception as e:
                self._emit_notif_test_result("discord", False, str(e))

        threading.Thread(target=_do, name="test-discord", daemon=True).start()

    @Slot()
    def testSlackWebhook(self):
        """Send test message to Slack webhook."""
        url = self._backend.config.get("slack_webhook", "")
        if not url:
            return

        def _do():
            try:
                resp = requests.post(url, json={
                    "text": "ScanHound test notification - Slack webhook is working!",
                }, timeout=10)
                if resp.status_code == 200:
                    self._emit_notif_test_result("slack", True, "Slack test sent")
                else:
                    self._emit_notif_test_result("slack", False, f"HTTP {resp.status_code}")
            except Exception as e:
                self._emit_notif_test_result("slack", False, str(e))

        threading.Thread(target=_do, name="test-slack", daemon=True).start()

    @Slot()
    def testPushover(self):
        """Send test message via Pushover."""
        user = self._backend.config.get("pushover_user", "")
        token = self._backend.config.get("pushover_token", "")
        if not user or not token:
            return

        def _do():
            try:
                resp = requests.post("https://api.pushover.net/1/messages.json", data={
                    "token": token,
                    "user": user,
                    "title": "ScanHound Test",
                    "message": "Pushover notifications are working!",
                }, timeout=10)
                data = resp.json()
                if data.get("status") == 1:
                    self._emit_notif_test_result("pushover", True, "Pushover test sent")
                else:
                    self._emit_notif_test_result("pushover", False, str(data.get("errors", "Unknown error")))
            except Exception as e:
                self._emit_notif_test_result("pushover", False, str(e))

        threading.Thread(target=_do, name="test-pushover", daemon=True).start()

    @Slot()
    def testWebhook(self):
        """Send test request to custom webhook."""
        url = self._backend.config.get("webhook_url", "")
        method = self._backend.config.get("webhook_method", "POST")
        if not url:
            return

        def _do():
            try:
                payload = {"event": "test", "message": "ScanHound webhook test"}
                if method.upper() == "GET":
                    resp = requests.get(url, params=payload, timeout=10)
                elif method.upper() == "PUT":
                    resp = requests.put(url, json=payload, timeout=10)
                else:
                    resp = requests.post(url, json=payload, timeout=10)

                if resp.status_code < 400:
                    self._emit_notif_test_result("webhook", True, f"Webhook test sent (HTTP {resp.status_code})")
                else:
                    self._emit_notif_test_result("webhook", False, f"HTTP {resp.status_code}")
            except Exception as e:
                self._emit_notif_test_result("webhook", False, str(e))

        threading.Thread(target=_do, name="test-webhook", daemon=True).start()

    @Slot()
    def testEmail(self):
        """Send test email via SMTP."""
        cfg = self._backend.config

        def _do():
            try:
                import smtplib
                from email.mime.text import MIMEText

                msg = MIMEText("ScanHound email notifications are working!")
                msg["Subject"] = "ScanHound Test Email"
                msg["From"] = cfg.get("email_from", "")
                msg["To"] = cfg.get("email_to", "")

                host = cfg.get("smtp_host", "")
                port = cfg.get("smtp_port", 587)

                with smtplib.SMTP(host, port, timeout=10) as server:
                    if cfg.get("smtp_tls", True):
                        server.starttls()
                    username = cfg.get("smtp_username", "")
                    password = cfg.get("smtp_password", "")
                    if username and password:
                        server.login(username, password)
                    server.send_message(msg)

                self._emit_notif_test_result("email", True, "Test email sent")
            except Exception as e:
                self._emit_notif_test_result("email", False, str(e))

        threading.Thread(target=_do, name="test-email", daemon=True).start()
